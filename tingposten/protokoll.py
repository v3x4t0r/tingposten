"""Parse one vedtak extract ("protokoll" PDF) from Bergen kommune.

Each PDF holds a single sak as treated by one organ:

    <tittel>
    Bergen bystyre behandlet saken i møtet 23.9.2026 sak 238/26 og fattet følgende vedtak:
    <vedtak>
    Bergen bystyres behandling
    Følgende representanter tok ordet: <talere>   (or "Ingen representanter tok ordet.")
    [Forslag fremsatt i bystyret: …] [Merknader fremsatt i bystyret: …]
    Votering:
    <one sentence per vote>
    <saksgang: earlier organs, newest first>

Anything missing gives an empty value, never an exception.
"""

import re

import pymupdf

_DATE = r"\d{1,2}\.\d{1,2}\.\d{4}"
_HEADER = re.compile(
    rf"(?m)^[ \t]*(?P<organ>[A-ZÆØÅ][^\n]*?)\s+behandlet\s+saken\s+i\s+møtet\s+(?P<dato>{_DATE})"
    r"\s+sak\s+(?P<saksnr>\d+/\d+)(?:\s+og\s+(?:fattet|traff)\s+følgende\s+vedtak)?\s*:"
)
_SAKSGANG = re.compile(
    rf"^(?P<organ>.*?)\s*(?P<verb>behandlet saken i møtet|hadde saken til orientering i møtet)"
    rf"\s+(?P<dato>{_DATE})\s+sak\s+(?P<saksnr>\d+/\d+)(?P<tail>.*)$"
)
_VOTE = re.compile(
    r"(?P<head>.+?),?\s+(?P<res>ble enstemmig vedtatt"
    r"|ble vedtatt med (?P<n1>\d+) stemmer \((?P<p1>[^)]*)\)"
    r"|fikk (?P<n2>\d+) stemmer \((?P<p2>[^)]*)\),? og var dermed falt)\s*\."
    r"(?:\s*\(det var \d+ representanter til stede\))?"
)
_TIL_STEDE = re.compile(r"\(det var (\d+) representanter til stede\)")
_PARTY = r"\d*[A-ZÆØÅ]{1,5}(?: [A-ZÆØÅ][a-zæøå]+)?"
_PARTY_LIST = re.compile(rf"{_PARTY}(?:(?:, | og ){_PARTY})*")
_LIST_MARKER = re.compile(r"^(?:\d+[.)]|[a-zæøå][.)]|•|[-–])(?:\s|$)")
# A line ending in "-" before one of these is a shared compound ("plan- og
# bygningsloven"), not a word split across lines.
_KEEP_SPACE = ("og ", "eller ", "samt ", "til ", "enn ")


def parse_pdf(path) -> dict:
    with pymupdf.open(path) as doc:
        return parse_pages([page.get_text() for page in doc])


def parse_pages(pages: list[str]) -> dict:
    text, starts = _join_pages(pages)
    result = {
        "tittel": "",
        "organ": "",
        "motedato": "",
        "saksnr": "",
        "vedtak": "",
        "vedtak_side": 0,
        "talere": [],
        "voteringer": [],
        "til_stede": None,
        "enstemmig": False,
        "innstilling_falt": False,
        "saksgang": [],
    }
    header = _HEADER.search(text)
    if not header:
        result["tittel"] = _flat(text[:300])
        return result
    result.update(
        tittel=_flat(text[: header.start()]),
        organ=_flat(header["organ"]),
        motedato=_iso(header["dato"]),
        saksnr=header["saksnr"],
        vedtak_side=sum(1 for s in starts if s <= header.end()),
    )

    # "Bergen bystyres behandling": the organ's own debate and votes.
    behandling = re.compile(rf"(?m)^[ \t]*{re.escape(result['organ'])}s\s+behandling[ \t]*$").search(
        text, header.end()
    )
    votering_at = text.find("Votering:", header.end())
    if behandling:
        vedtak_end = behandling.start()
    else:
        first = _saksgang_start(text, header.end())
        vedtak_end = first if first is not None else len(text)
    result["vedtak"] = _tidy(text[header.end() : vedtak_end])

    after = behandling.end() if behandling else vedtak_end
    rest_start = votering_at + len("Votering:") if votering_at >= 0 else after
    saksgang_at = _saksgang_start(text, rest_start)
    section_end = saksgang_at if saksgang_at is not None else len(text)

    result["talere"] = _talere(text[after : votering_at if votering_at >= 0 else section_end])
    if votering_at >= 0:
        block = _flat(text[rest_start:section_end])
        result["voteringer"] = [_vote(m) for m in _VOTE.finditer(block)]
        if m := _TIL_STEDE.search(block):
            result["til_stede"] = int(m[1])
    if saksgang_at is not None:
        result["saksgang"] = _saksgang(text[saksgang_at:])

    votes = result["voteringer"]
    result["enstemmig"] = bool(votes) and all(v["enstemmig"] for v in votes)
    result["innstilling_falt"] = _innstilling_falt(votes, result["saksgang"])
    return result


def _innstilling_falt(votes: list[dict], saksgang: list[dict]) -> bool:
    innstilling = [v for v in votes if v["forslag"].lower().startswith("innstilling")]
    if any(v["resultat"] == "falt" for v in innstilling):
        return True
    # An adopted alternativforslag replaces the innstilling, which is then
    # often not voted on at all (e.g. 258/26). Only counts when the sak had one.
    had_innstilling = any(s["type"] == "innstilling" for s in saksgang)
    alternativ_won = any(
        v["forslag"].lower().startswith("alternativ") and v["resultat"] == "vedtatt" for v in votes
    )
    adopted = any(v["resultat"] == "vedtatt" for v in innstilling)
    return had_innstilling and alternativ_won and not adopted


def _vote(m: re.Match) -> dict:
    head = m["head"].strip(" ,")
    fremsatt_av = None
    if fm := re.search(r"\s+fremsatt av (.+)$", head):
        fremsatt_av = fm[1].strip(" ,")
        head = head[: fm.start()]
    fra = []
    if (i := head.rfind(" fra ")) >= 0:
        fra_text = head[i + 5 :].strip(" ,")
        head = head[:i]
        fra = re.split(r", | og ", fra_text) if _PARTY_LIST.fullmatch(fra_text) else [fra_text]
    enstemmig = "enstemmig" in m["res"]
    stemmer = m["n1"] or m["n2"]
    partier = m["p1"] if m["p1"] is not None else m["p2"]
    return {
        "forslag": head.strip(" ,"),
        "fra": fra,
        "fremsatt_av": fremsatt_av,
        "resultat": "falt" if m["n2"] else "vedtatt",
        "enstemmig": enstemmig,
        "stemmer": None if enstemmig else int(stemmer),
        "partier": [p.strip() for p in (partier or "").split(",") if p.strip()],
        "tekst": m[0].strip(),
    }


def _talere(section: str) -> list[str]:
    if "Ingen representanter tok ordet" in section:
        return []
    i = section.find("tok ordet:")
    if i < 0:
        return []
    names = re.split(r"\n[ \t]*\n", section[i + len("tok ordet:") :].strip(), maxsplit=1)[0]
    return [n.strip() for n in re.findall(r"([^,()]+?\([^()]+\))", _flat(names))]


def _paragraphs(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in re.split(r"(\n[ \t]*\n)", text):
        if part.strip() and not re.fullmatch(r"\n[ \t]*\n", part):
            out.append((pos, _flat(part)))
        pos += len(part)
    return out


def _saksgang_start(text: str, start: int) -> int | None:
    for offset, para in _paragraphs(text[start:]):
        if _SAKSGANG.match(para):
            return start + offset
    return None


def _saksgang(text: str) -> list[dict]:
    paras = [p for _, p in _paragraphs(text)]
    out = []
    for i, para in enumerate(paras):
        m = _SAKSGANG.match(para)
        if not m:
            continue
        organ = m["organ"]
        if ". " in organ:
            organ = organ.rsplit(". ", 1)[1]
        organ = re.sub(r"^\d+\s+", "", organ).strip(" ,")
        # The header's own wording decides. The next paragraph only fills in a
        # bare header, and only if it is the same organ speaking
        # ("Byrådet behandlet saken … sak 156/26" + "Byrådet innstiller …").
        tail = m["tail"][:120]
        following = paras[i + 1][:120] if i + 1 < len(paras) else ""
        said = f"{tail} {following}" if following.startswith(organ) else tail
        if m["verb"].startswith("hadde") or "tok saken til orientering" in tail:
            kind = "orientering"
        elif "innstilling" in tail[:60]:
            kind = "innstilling"
        elif re.search(r"(?:fattet|traff) følgende vedtak", tail[:60]):
            kind = "vedtak"
        elif "innstiller" in said:
            kind = "innstilling"
        elif "tok saken til orientering" in said:
            kind = "orientering"
        else:
            kind = "annet"
        out.append({"organ": organ, "dato": _iso(m["dato"]), "saksnr": m["saksnr"], "type": kind})
    return out


def _join_pages(pages: list[str]) -> tuple[str, list[int]]:
    """Pages joined into one text, page numbers removed; and each page's start offset."""
    text, starts = "", []
    for page in pages:
        lines = [line.rstrip() for line in page.split("\n")]
        filled = [i for i, line in enumerate(lines) if line.strip()]
        for i in filled[:2] + filled[-2:]:
            if re.fullmatch(r"\s*\d{1,3}\s*", lines[i]):
                lines[i] = ""
        body = "\n".join(lines).strip("\n")
        if text:
            # Keep a paragraph break only if the previous page ended a sentence.
            text += "\n\n" if re.search(r"[.:;!?)]\s*$", text) else "\n"
        starts.append(len(text))
        text += body
    return text, starts


def _flat(text: str) -> str:
    text = re.sub(r"(\w)-[ \t]*\n\s*(?=\w)(?!(?:og|eller|samt|til|enn)\b)", r"\1-", text)
    return re.sub(r"\s+", " ", text).strip()


def _tidy(text: str) -> str:
    """Whitespace-normalised text: one line per paragraph or list item."""
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        prev = lines[-1] if lines else ""
        if prev in ("•", "-", "–"):
            lines[-1] = f"{prev} {line}"
        elif not prev or _LIST_MARKER.match(line):
            lines.append(line)
        elif prev.endswith("-") and not line.startswith(_KEEP_SPACE) and re.match(r"\w", line):
            lines[-1] = prev + line
        else:
            lines[-1] = f"{prev} {line}"
    paras = [re.sub(r"\s+", " ", line) for line in lines if line.strip()]
    return "\n".join(paras)


def _iso(date: str) -> str:
    day, month, year = date.split(".")
    return f"{year}-{int(month):02d}-{int(day):02d}"
