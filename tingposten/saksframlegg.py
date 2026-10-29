"""Parse a saksframlegg (saksframstilling) PDF into sections and signals.

Headings are found from layout, not wording: a line that is (almost) all
bold, at least 11 pt and starts at the page's left margin. Bold cells in
tables sit a few points in from the margin, which keeps them out. Titles
are 12–14 pt bold on page 1; everything above the title (letterhead,
"Vår referanse", saksbehandler) is header and dropped.

parse_pdf() never raises: a missing or broken file gives empty fields.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

BYDELER = ("Arna", "Bergenhus", "Fana", "Fyllingsdalen", "Laksevåg", "Ytrebygda", "Årstad", "Åsane")

# Showing a bydel (map, badge) says "this is where it happens", so a passing mention is not enough
# (e.g. "klassedeling i Arna" as one line in a city-wide school budget).
MIN_TREFF = 3
WHOLE_CITY = re.compile(r"hele byen|hele bergen|alle bydeler|byomfattende|bergensskolen", re.I)


def saksbydeler(item: dict, saksframlegg: dict) -> tuple[list[str], str]:
    """(bydeler, grunnlag) a reader is shown for the sak. A bydel in the title (plan saker always
    start with one) decides alone. Otherwise a bydel must be named at least
    MIN_TREFF times in the saksframlegg; city-wide saker, or ones where most
    bydeler qualify, get no map."""
    tittel = item.get("tittel", "")
    in_title = [b for b in BYDELER if re.search(rf"(?<!\w){b}(?!\w)", tittel)]
    if in_title:
        return in_title, "tittel"
    if WHOLE_CITY.search(tittel):
        return [], ""
    treff = saksframlegg.get("bydel_treff") or {}
    many = [b for b in BYDELER if treff.get(b, 0) >= MIN_TREFF]
    if len(many) > len(BYDELER) // 2:
        return [], ""
    return many, "saksframlegg"


_HEADER_WORDS = re.compile(r"^(Byrådssak|Bystyresak|Saksframstilling|Intern korrespondanse)\b")
_BULLET = re.compile(r"^(?:[•▪◦·\-–]\s*|\d{1,2}[.)]\s+|[a-h][.)]\s+)")
_SYMBOL_BULLET = re.compile(r"^[•▪◦·\-–]")
_PRIVATE_BULLETS = str.maketrans({"": "•", "": "▪", "": "•", " ": " ", " ": " "})
_REFERANSE = re.compile(r"\b(\d{4}/\d{3,8}-\d+)\b")
_SIGNATURE_START = re.compile(r"^Dato:\s")
_SIGNATURE_END = re.compile(r"godkjent elektronisk", re.I)


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    y1: float
    size: float
    bold: bool
    text: str
    spans: list = field(default_factory=list)  # [(text, bold)]; bold None = inserted separator


def parse_pdf(path) -> dict:
    try:
        with pymupdf.open(Path(path)) as doc:
            return parse_lines(extract_lines(doc), doc.page_count)
    except Exception as e:  # a broken PDF must not stop a run
        log.warning("could not parse %s: %s", path, e)
        return _empty(0)


def extract_lines(doc) -> list[Line]:
    """Visual lines in reading order, page numbers removed."""
    out = []
    for pno, page in enumerate(doc, 1):
        height = page.rect.height
        raw = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [(s["text"].translate(_PRIVATE_BULLETS), _is_bold(s), s["size"]) for s in line["spans"]]
                if not any(t.strip() for t, _, _ in spans):
                    continue
                x0, y0, x1, y1 = line["bbox"]
                raw.append((y0, x0, y1, spans))
        raw.sort(key=lambda r: (r[0], r[1]))
        groups: list[list] = []
        for item in raw:  # pieces of the same visual line: bullet + text, table cells
            if groups and abs(item[0] - groups[-1][0][0]) <= 2.5:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            group.sort(key=lambda r: r[1])
            spans = []
            for k, (_, _, _, sp) in enumerate(group):
                if k:
                    spans.append((" ", None))  # gap between separate pieces of the visual line
                spans += [(t, b) for t, b, _ in sp]
            text = re.sub(r"\s+", " ", "".join(t for t, _ in spans)).strip()
            y1 = max(g[2] for g in group)
            if text.isdigit() and (y1 > height - 60 or group[0][0] < 60):
                continue  # page number
            chars = [(len(t.strip()), b) for t, b in spans if t.strip() and b is not None]
            bold_share = sum(n for n, b in chars if b) / max(1, sum(n for n, _ in chars))
            out.append(
                Line(
                    page=pno,
                    x0=group[0][1],
                    y0=group[0][0],
                    y1=y1,
                    size=round(max(s for _, _, _, sp in group for _, _, s in sp), 1),
                    bold=bold_share >= 0.8,
                    text=text,
                    spans=spans,
                )
            )
    return out


def _is_bold(span: dict) -> bool:
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()


def parse_lines(lines: list[Line], pages: int) -> dict:
    result = _empty(pages)
    if not lines:
        return result
    first = [l for l in lines if l.page == 1]
    ref = next((m.group(1) for l in first if (m := _REFERANSE.search(l.text))), "")
    result["referanse"] = ref

    margin = _left_margin(lines)
    title_start, start, title = _title(lines, margin)
    result["tittel"] = title
    body = _split_inline_headings(lines[start:], margin)

    sections = _sections(body, margin)
    result["seksjoner"] = [{"overskrift": s["overskrift"], "tekst": _render(s["lines"]), "side": s["side"]} for s in sections]
    by_heading = [(s["overskrift"].lower(), s) for s in result["seksjoner"]]

    result["hva_saken_gjelder"] = _pick(by_heading, (r"^(hva saken gjelder|kva saka gjeld)",))
    result["begrunnelse"] = _pick(
        by_heading,
        (
            r"^byrådets vurdering",
            r"^byrådet si vurdering",
            r"^sekretariatets vurdering",
            r"^administrasjonens vurdering",
            r"vurdering og (?:anbefaling|tilråding)",
            r"^vurdering",
            r"^begrunnelse(?! for framleggelse)",
            r"konklusjon",
        ),
    )
    result["forslag_til_vedtak"] = _pick(
        by_heading,
        (
            r"innstiller til bystyret",
            r"^forslag til innstilling",
            r"^innstilling til (?:bergen )?bystyre",
            r"^forslag til vedtak",
            r"\binnstiller\b",
        ),
    )
    result["vedlegg"] = _vedlegg(sections)

    pages_text: dict[int, list[str]] = {}
    for l in lines[title_start:]:
        pages_text.setdefault(l.page, []).append(l.text)
    full = {p: " ".join(t) for p, t in pages_text.items()}
    result["belop"] = [a for p, text in sorted(full.items()) for a in find_amounts(text, p)]
    result["hjemler"] = _dedupe(a for p, text in sorted(full.items()) for a in find_hjemler(text, p))
    all_text = " ".join(full.values())
    treff = {b: len(re.findall(rf"\b{b}s?\b", all_text)) for b in BYDELER}
    result["bydeler"] = sorted(b for b, n in treff.items() if n)
    result["bydel_treff"] = {b: n for b, n in treff.items() if n}
    return result


def _empty(pages: int) -> dict:
    return {
        "tittel": "",
        "referanse": "",
        "sider": pages,
        "seksjoner": [],
        "hva_saken_gjelder": "",
        "begrunnelse": "",
        "forslag_til_vedtak": "",
        "belop": [],
        "hjemler": [],
        "bydeler": [],
        "bydel_treff": {},
        "vedlegg": [],
    }


def _left_margin(lines: list[Line]) -> float:
    xs = Counter(round(l.x0) for l in lines if not l.bold and 10.5 <= l.size <= 11.5)
    if not xs:
        return min(round(l.x0) for l in lines)
    floor = max(3, 0.1 * sum(xs.values()))
    return min(x for x, n in xs.items() if n >= floor)


def _title(lines: list[Line], margin: float) -> tuple[int, int, str]:
    """Index of the title's first line, of the first body line, and the title."""
    for i, l in enumerate(lines):
        if l.page != 1:
            break
        if (
            l.bold
            and l.size >= 10.9
            and abs(l.x0 - margin) <= 3
            and not _HEADER_WORDS.match(l.text)
            and not l.text.endswith(":")
        ):
            parts, j = [l.text], i + 1
            while (
                j < len(lines)
                and lines[j].page == l.page
                and lines[j].bold
                and lines[j].size >= 10.9
                and not lines[j].text.endswith(":")
                and lines[j].y0 - lines[j - 1].y1 < min(lines[j].size, lines[j - 1].size) * 0.8
            ):
                parts.append(lines[j].text)
                j += 1
            return i, j, " ".join(parts)
    return 0, 0, ""


def _split_inline_headings(lines: list[Line], margin: float) -> list[Line]:
    """'Vedlegg: 1. Kart' with only 'Vedlegg:' bold → heading line + text line."""
    out = []
    for l in lines:
        prefix, rest, in_prefix = "", "", True
        for text, bold in l.spans:
            if in_prefix and (bold or bold is None or not text.strip()):
                prefix += text
            else:
                in_prefix = False
                rest += text
        prefix, rest = prefix.strip(), rest.strip()
        if (
            not l.bold
            and prefix.endswith(":")
            and 3 <= len(prefix) <= 80
            and rest
            and abs(l.x0 - margin) <= 3
        ):
            out.append(Line(l.page, l.x0, l.y0, l.y1, l.size, True, prefix, [(prefix, True)]))
            out.append(Line(l.page, l.x0, l.y0 + 0.01, l.y1, l.size, False, rest, [(rest, False)]))
        else:
            out.append(l)
    return out


def _is_heading(l: Line, margin: float) -> bool:
    if not l.bold or l.size < 10.9 or len(l.text) > 160 or _SYMBOL_BULLET.match(l.text):
        return False
    if len(re.findall(r"[A-Za-zÆØÅæøå]", l.text)) < 2:
        return False
    return l.size >= 11.9 and l.x0 < 200 or abs(l.x0 - margin) <= 3


def _sections(body: list[Line], margin: float) -> list[dict]:
    sections = [{"overskrift": "", "side": body[0].page if body else 1, "lines": []}]
    prev_heading: Line | None = None
    for l in body:
        if _is_heading(l, margin):
            cur = sections[-1]
            if (
                prev_heading is not None
                and not cur["lines"]
                and not prev_heading.text.endswith(":")
                and abs(prev_heading.size - l.size) < 0.6
                and l.page == prev_heading.page
                and l.y0 - prev_heading.y1 < l.size * 0.8
            ):
                cur["overskrift"] += " " + l.text  # heading wrapped over two lines
            else:
                sections.append({"overskrift": l.text, "side": l.page, "lines": []})
            prev_heading = l
        else:
            sections[-1]["lines"].append(l)
            prev_heading = None
    for s in sections:
        s["overskrift"] = s["overskrift"].strip()
        s["lines"] = _drop_signature(s["lines"])
    return [s for s in sections if s["overskrift"] or s["lines"]]


def _drop_signature(lines: list[Line]) -> list[Line]:
    """Remove 'Dato: … / name – title / Dokumentet er godkjent elektronisk.'"""
    out, skipping = [], False
    for l in lines:
        if _SIGNATURE_START.match(l.text):
            skipping = True
        if not skipping:
            out.append(l)
        if skipping and _SIGNATURE_END.search(l.text):
            skipping = False
    return out


def _render(lines: list[Line]) -> str:
    """Join wrapped lines into paragraphs; bullet items on their own lines."""
    blocks: list[list] = []  # [kind, text]
    prev: Line | None = None
    for l in lines:
        is_item = bool(_BULLET.match(l.text))
        new_page = prev is not None and l.page != prev.page
        gap = (l.y0 - prev.y1) if prev is not None and not new_page else 0
        if prev is None or is_item:
            blocks.append(["li" if is_item else "p", l.text])
        elif new_page:
            if re.search(r"[.:!?»)]$", prev.text):
                blocks.append(["p", l.text])
            else:
                blocks[-1][1] += " " + l.text
        elif gap > prev.size * 0.4:
            blocks.append(["p", l.text])
        else:
            blocks[-1][1] += " " + l.text
        prev = l
    parts = []
    for i, (kind, text) in enumerate(blocks):
        text = re.sub(r"^[•▪◦·]\s*", "• ", text)
        if i:
            parts.append("\n" if kind == "li" and blocks[i - 1][0] == "li" else "\n\n")
        parts.append(text)
    return "".join(parts)


def _pick(by_heading: list[tuple[str, dict]], patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        for heading, section in by_heading:
            if re.search(pattern, heading):
                return section["tekst"]
    return ""


def _vedlegg(sections: list[dict]) -> list[str]:
    for s in sections:
        if re.fullmatch(r"vedlegg\s*:?", s["overskrift"].strip(), re.I):
            if any(_BULLET.match(l.text) for l in s["lines"]):
                blocks = _render(s["lines"]).split("\n")
            else:  # unnumbered list: one attachment per line, lowercase start continues the previous
                blocks = []
                for l in s["lines"]:
                    if blocks and l.text[:1].islower():
                        blocks[-1] += " " + l.text
                    else:
                        blocks.append(l.text)
            items = [re.sub(r"^(?:[•▪◦·\-–]\s*|\d{1,2}[.)]\s*)", "", b).strip() for b in blocks]
            return [i for i in items if i]
    return []


# --- amounts -----------------------------------------------------------------

_SEP = r"[   ]"
_NUM = rf"(?<![\d,.])(?:\d{{1,3}}(?:(?:{_SEP}|\.)\d{{3}})+(?:,\d+)?|\d+(?:[,.]\d+)?)(?![\d])"
_SCALE = r"(?:mill(?:ion(?:er|ar)?(?![a-zæøå])|\.|(?![a-zæøå]))|mrd(?:\.|(?![a-zæøå]))|milliard(?:er|ar)?(?![a-zæøå])|tusen(?![a-zæøå])|mnok(?![a-z]))"
_CUR = r"(?:kroner(?![a-zæøå])|kr(?:\.|(?![a-zæøå]))|(?-i:NOK)(?![A-Za-z]))"
_S = r"[\s  ]*"
_AMOUNT = re.compile(
    rf"(?:{_CUR}{_S}(?P<n1>{_NUM}){_S}(?P<s1>{_SCALE})?(?:{_S}{_CUR})?"  # kr 1,5 mill. / kr 500 000
    rf"|mnok{_S}(?P<n2>{_NUM})"  # MNOK 47
    rf"|(?P<n3>{_NUM}){_S}(?P<s3>{_SCALE})(?:{_S}{_CUR})?"  # 69 mill. (kroner)
    rf"|(?P<n4>{_NUM}){_S}{_CUR}(?!{_S}\d))"  # 500 000 kroner (not "for 2026 kr 26 900")
    r"(?:,-)?",
    re.I,
)
_NOT_MONEY = re.compile(
    r"^[\s ]*(?:innbyggere|personer|mennesker|tonn|passasjerer|reisende|kvadratmeter|m2|m²|m3|kubikk|liter"
    r"|km|kilometer|år|besøk|brukere|kunder|elever|kwh|kw|reiser|kjøretøy|biler|turer)\b",
    re.I,
)
# Longest prefix first: "milliarder" also starts with "mill".
_MULT = (("milliard", 1e9), ("mrd", 1e9), ("mnok", 1e6), ("mill", 1e6), ("tusen", 1e3))


def find_amounts(text: str, page: int = 1) -> list[dict]:
    out = []
    for m in _AMOUNT.finditer(text):
        num = m.group("n1") or m.group("n2") or m.group("n3") or m.group("n4")
        scale = (m.group("s1") or m.group("s3") or ("mnok" if m.group("n2") else "")).lower()
        has_currency = bool(m.group("n2") or re.search(_CUR, m.group(0), re.I))
        if not has_currency and _NOT_MONEY.match(text[m.end():]):
            continue
        value = _number(num)
        mult = next((v for k, v in _MULT if scale.startswith(k)), 1.0)
        out.append({"tekst": re.sub(r"[\s  ]+", " ", m.group(0)).strip(), "kroner": int(round(value * mult)), "side": page})
    return out


def _number(text: str) -> float:
    t = re.sub(r"[   ]", "", text)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        t = t.replace(".", "")
    return float(t.replace(",", "."))


# --- legal references --------------------------------------------------------

_LAW_WORD = r"[A-Za-zÆØÅæøå]+(?:loven|lova|forskriften|forskrifta)"
_LAW = rf"(?:(?:[A-Za-zÆØÅæøå]+-\s(?:og|eller)\s)?{_LAW_WORD}|pbl\.?)"
_PAR = r"\d+(?:-\d+)?[a-z]?"
_PARS = rf"{_PAR}(?:\s*(?:,|og|–|til)\s*{_PAR}(?!\.?\s*(?:ledd|punktum|bokstav)))*"
_HJEMMEL = re.compile(
    rf"(?<![A-Za-zÆØÅæøå])(?P<lov>{_LAW})\s*(?:\([^)]{{1,12}}\)\s*)?(?P<sign>§§?)\s*(?P<par>{_PARS})"
    rf"|(?P<sign2>§§?)\s*(?P<par2>{_PARS})\s+(?:i|etter)\s+(?P<lov2>{_LAW})(?![A-Za-zÆØÅæøå])"
)


def find_hjemler(text: str, page: int = 1) -> list[dict]:
    out = []
    for m in _HJEMMEL.finditer(text):
        lov = (m.group("lov") or m.group("lov2")).lower()
        sign = m.group("sign") or m.group("sign2")
        par = re.sub(r"\s+", " ", m.group("par") or m.group("par2")).strip()
        if lov.startswith("pbl"):
            lov = "plan- og bygningsloven"
        out.append({"tekst": f"{lov} {sign} {par}", "side": page})
    return out


def _dedupe(items) -> list[dict]:
    seen, out = set(), []
    for i in items:
        if i["tekst"] not in seen:
            seen.add(i["tekst"])
            out.append(i)
    return out
