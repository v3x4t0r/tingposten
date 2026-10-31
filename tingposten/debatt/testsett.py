"""The moderation test: one set of Norwegian test comments, run through each
moderator, scored against the owner's labels (go/no-go before comments go live).

  data/debatt/testsett.jsonl   the comments: id, sak, sak_tittel, sak_ingress, forelder,
                               tekst, forslag (the drafter's label), grense, merknad
  data/debatt/fasit.json       the owner's label per id (`tingposten debatt-fasit`);
                               until an id is labelled, its `forslag` counts, and the
                               report says how many were checked
  data/debatt/test/<moderator>-<time>.jsonl   one line per comment and run

The report scores each run at several thresholds from the stored probabilities,
so one run (and one bill) is enough to choose DEBATT_TERSKEL.
"""

import json
import logging
import os
import statistics
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config
from .moderering import Moderator, ModereringFeil, Sak, Vurdering, avgjor
from .regler import ALVORLIGE, REGLER

log = logging.getLogger(__name__)

TERSKLER = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)


def sett_sti(d: Path | None = None) -> Path:
    return (d or config.DEBATT_DIR) / "testsett.jsonl"


def fasit_sti(d: Path | None = None) -> Path:
    return (d or config.DEBATT_DIR) / "fasit.json"


def les_sett(d: Path | None = None) -> list[dict]:
    path = sett_sti(d)
    if not path.exists():
        raise FileNotFoundError(f"no test set at {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def les_fasit(d: Path | None = None) -> dict[str, dict]:
    path = fasit_sti(d)
    return json.loads(path.read_text()) if path.exists() else {}


def fasit_for(item: dict, fasit: dict[str, dict]) -> tuple[str, bool]:
    """(the label, whether the owner set it)."""
    if item["id"] in fasit:
        return fasit[item["id"]]["regel"], True
    return item["forslag"], False


def kjor(moderator: Moderator, sett: list[dict], d: Path | None = None, bare: int | None = None) -> Path:
    """Run every comment through one moderator; one result line each. A comment the
    moderator can't judge is recorded as an error (it would wait for the editor)."""
    ut_dir = (d or config.DEBATT_DIR) / "test"
    ut_dir.mkdir(parents=True, exist_ok=True)
    navn = moderator.name.replace(":", "_").replace("/", "_")
    ut = ut_dir / f"{navn}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    with ut.open("w") as f:
        for i, item in enumerate(sett[:bare] if bare else sett, 1):
            sak = Sak(item["sak"], item.get("sak_tittel", ""), item.get("sak_ingress", ""))
            try:
                rad = {"id": item["id"], **moderator.vurder(item["tekst"], sak, item.get("forelder")).som_dict()}
            except ModereringFeil as e:
                rad = {"id": item["id"], "feil": str(e)}
            f.write(json.dumps(rad, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0:
                log.info("%s: %d/%d", moderator.name, i, len(sett))
    return ut


def les_resultat(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rapport(resultat: list[dict], sett: list[dict], fasit: dict[str, dict], stol_pa_etikett: bool = False) -> dict:
    """Scores for one run: how often the rule matched, and per threshold what would
    have been published, stopped and sent to the editor, and what went wrong."""
    etter_id = {i["id"]: i for i in sett}
    rader = [r for r in resultat if r["id"] in etter_id]
    svar = [r for r in rader if "feil" not in r]
    kontrollert = sum(1 for r in rader if r["id"] in fasit)
    ut = {
        "n": len(rader), "kontrollert": kontrollert, "feil": len(rader) - len(svar),
        "modell": svar[0]["modell"] if svar else None,
        "regel_treff": _andel(sum(1 for r in svar if r["regel"] == fasit_for(etter_id[r["id"]], fasit)[0]), len(svar)),
        "ms_p50": round(statistics.median([r["ms"] for r in svar])) if svar else None,
        "ms_p95": _p95([r["ms"] for r in svar]),
        "kr_per_1000": round(sum(r.get("kostnad_usd", 0) for r in svar) / max(len(svar), 1) * 1000 * config_kurs(), 2),
        "kalibrert": bool(svar) and all(r["sannsynligheter"] is not None for r in svar),
        "terskler": {},
    }
    terskler = TERSKLER if ut["kalibrert"] else (None,)
    for t in terskler:
        telling = {"publiser": 0, "avvis": 0, "redaktor": 0, "farlige_slipp": 0, "feil_stoppet": 0,
                   "feil_publisert": 0, "feil_regel": 0}
        slipp: list[str] = []
        stoppet: list[str] = []
        for r in rader:
            riktig = fasit_for(etter_id[r["id"]], fasit)[0]
            if "feil" in r:
                telling["redaktor"] += 1
                continue
            v = Vurdering(r["regel"], r["sannsynligheter"], r["modell"], r["ms"])
            utfall = avgjor(v, t if t is not None else 1.0, stol_pa_etikett)
            telling[utfall] += 1
            if utfall == "publiser" and riktig != "ok":
                telling["feil_publisert"] += 1
                if riktig in ALVORLIGE:
                    telling["farlige_slipp"] += 1
                    slipp.append(r["id"])
            elif utfall == "avvis" and riktig == "ok":
                telling["feil_stoppet"] += 1
                stoppet.append(r["id"])
            elif utfall == "avvis" and r["regel"] != riktig:
                telling["feil_regel"] += 1
        telling["andel_redaktor"] = _andel(telling["redaktor"], len(rader))
        ut["terskler"][str(t) if t is not None else "etikett"] = {**telling, "slipp": slipp, "stoppet": stoppet}
    return ut


def config_kurs() -> float:
    """NOK per USD for the report's cost column (rough; the bill is in dollars)."""
    return float(os.environ.get("TINGPOSTEN_NOK_PER_USD", "10.5"))


def _andel(teller: int, nevner: int) -> float | None:
    return round(teller / nevner, 3) if nevner else None


def _p95(verdier: list[int]) -> int | None:
    if not verdier:
        return None
    s = sorted(verdier)
    return s[min(len(s) - 1, round(0.95 * (len(s) - 1)))]


def tabell(rapporter: dict[str, dict]) -> str:
    """The reports as a Markdown table, one row per moderator and threshold."""
    linjer = ["| moderator | terskel | regel riktig | publisert | stoppet | til redaktør | farlige slipp | "
              "feil stoppet (ok) | feil publisert | p50 ms | p95 ms | kr/1000 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for navn, r in rapporter.items():
        for t, s in r["terskler"].items():
            linjer.append(
                f"| {navn} | {t} | {_pst(r['regel_treff'])} | {s['publiser']} | {s['avvis']} | {s['redaktor']} "
                f"({_pst(s['andel_redaktor'])}) | {s['farlige_slipp']} | {s['feil_stoppet']} | {s['feil_publisert']} "
                f"| {r['ms_p50']} | {r['ms_p95']} | {r['kr_per_1000']} |")
    return "\n".join(linjer)


def _pst(x: float | None) -> str:
    return "–" if x is None else f"{round(x * 100)} %"


# --- labelling page -------------------------------------------------------------

def lagre_fasit(d: Path | None, id_: str, regel: str, merknad: str = "") -> dict:
    if regel not in REGLER:
        raise ValueError("ukjent regel")
    path = fasit_sti(d)
    fasit = les_fasit(d)
    fasit[id_] = {"regel": regel, "merknad": merknad.strip()[:500],
                  "tid": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fasit, ensure_ascii=False, indent=1))
    os.replace(tmp, path)
    return fasit[id_]


def nyeste_kjoringer(d: Path | None = None) -> dict[str, Path]:
    """The newest run file per moderator."""
    test_dir = (d or config.DEBATT_DIR) / "test"
    nyeste: dict[str, Path] = {}
    for path in sorted(test_dir.glob("*.jsonl")) if test_dir.exists() else []:
        nyeste[path.stem.rsplit("-", 2)[0]] = path
    return nyeste


def siste_resultater(d: Path | None = None) -> dict[str, dict[str, dict]]:
    """The newest run per moderator: {moderator: {id: result}}, for the page's 'what the models said'."""
    return {navn: {r["id"]: r for r in les_resultat(p)} for navn, p in nyeste_kjoringer(d).items()}


def make_server(port: int = 8766, d: Path | None = None) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path != "/":
                return self._send(404, "text/plain; charset=utf-8", b"Ikke funnet")
            data = {"sett": les_sett(d), "fasit": les_fasit(d), "modeller": siste_resultater(d),
                    "regler": {k: list(v) for k, v in REGLER.items()}}
            body = SIDE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
            self._send(200, "text/html; charset=utf-8", body.encode())

        def do_POST(self):
            if self.path != "/lagre":
                return self._send(404, "text/plain; charset=utf-8", b"Ikke funnet")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 4000:
                    raise ValueError("for stor")
                body = json.loads(self.rfile.read(length))
                if body.get("id") not in {i["id"] for i in les_sett(d)}:
                    raise ValueError("ukjent kommentar")
                with lock:
                    entry = lagre_fasit(d, body["id"], body.get("regel"), str(body.get("merknad") or ""))
            except (ValueError, json.JSONDecodeError, KeyError) as e:
                return self._send(400, "application/json", json.dumps({"feil": str(e)}).encode())
            self._send(200, "application/json", json.dumps(entry, ensure_ascii=False).encode())

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


SIDE = r"""<!doctype html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fasit · debattreglene</title>
<style>
:root { --bg: #f6f5f1; --card: #fff; --fg: #1d1d1b; --muted: #66655f; --line: #e0ded6; --accent: #1f5f8b;
  --ok: #2b7a4b; --stopp: #9a3b2e; --warn-bg: #fdf0d2; }
@media (prefers-color-scheme: dark) { :root { --bg: #141413; --card: #1e1e1c; --fg: #ecebe6; --muted: #a3a29b;
  --line: #34332f; --accent: #7fb5dd; --ok: #7ccb9b; --stopp: #e5927f; --warn-bg: #33280f; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; }
header { position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--line); padding: 10px 16px; z-index: 1; }
header div { max-width: 46rem; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; justify-content: space-between; }
h1 { font-size: 1rem; margin: 0; }
.sub { color: var(--muted); font-size: .85rem; }
main { max-width: 46rem; margin: 0 auto; padding: 16px; }
details.regler { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; margin-bottom: 16px; font-size: .9rem; }
article { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin: 0 0 14px; }
article.satt { border-left: 4px solid var(--accent); }
.meta { color: var(--muted); font-size: .8rem; display: flex; flex-wrap: wrap; gap: 8px; }
.grense { background: var(--warn-bg); border-radius: 99px; padding: 0 8px; }
.forelder { border-left: 3px solid var(--line); padding-left: 10px; color: var(--muted); font-size: .9rem; margin: 8px 0; white-space: pre-wrap; }
.tekst { white-space: pre-wrap; margin: 8px 0; font-size: 1.02rem; }
.merknad { color: var(--muted); font-size: .85rem; font-style: italic; }
.valg { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.valg button { font: inherit; font-size: .85rem; border: 1px solid var(--line); background: var(--card); color: var(--fg);
  border-radius: 99px; padding: 3px 12px; cursor: pointer; }
.valg button[aria-pressed="true"] { background: var(--fg); color: var(--bg); border-color: var(--fg); }
.valg button.ok[aria-pressed="true"] { background: var(--ok); border-color: var(--ok); }
.modeller { font-size: .8rem; color: var(--muted); margin-top: 8px; }
.modeller .uenig { color: var(--stopp); font-weight: 600; }
.filter button { font: inherit; font-size: .8rem; border: 1px solid var(--line); background: var(--card); color: var(--fg); border-radius: 99px; padding: 2px 10px; cursor: pointer; }
.filter button[aria-pressed="true"] { background: var(--fg); color: var(--bg); }
</style>
</head>
<body>
<header><div><h1>Fasit for debattreglene</h1><span class="sub" id="telling"></span>
<span class="filter" role="group" aria-label="Vis"><button data-f="alle" aria-pressed="true">Alle</button>
<button data-f="usatt">Ikke satt</button><button data-f="grense">Grensetilfeller</button>
<button data-f="uenig">Modellene er uenige</button></span></div></header>
<main>
<details class="regler"><summary>Reglene</summary><dl id="regler"></dl></details>
<div id="liste"></div>
</main>
<script>
const DATA = __DATA__;
const $ = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
let filter = "alle";
for (const [k, [navn, tekst]] of Object.entries(DATA.regler)) {
  document.getElementById("regler").append($("dt", null, `${k} – ${navn}`), $("dd", null, tekst));
}
function fasit(item) { return (DATA.fasit[item.id] || {}).regel || item.forslag; }
function uenig(item) { return Object.values(DATA.modeller).some(m => m[item.id] && m[item.id].regel && m[item.id].regel !== fasit(item)); }
function tegn() {
  const liste = document.getElementById("liste"); liste.replaceChildren();
  const satt = DATA.sett.filter(i => DATA.fasit[i.id]).length;
  document.getElementById("telling").textContent = `${satt} av ${DATA.sett.length} satt`;
  for (const item of DATA.sett) {
    if (filter === "usatt" && DATA.fasit[item.id]) continue;
    if (filter === "grense" && !item.grense) continue;
    if (filter === "uenig" && !uenig(item)) continue;
    const a = $("article", DATA.fasit[item.id] ? "satt" : "");
    const meta = $("div", "meta"); meta.append($("span", null, item.id), $("span", null, item.sak_tittel));
    if (item.grense) meta.append($("span", "grense", "grensetilfelle"));
    a.append(meta);
    if (item.forelder) a.append($("div", "forelder", item.forelder));
    a.append($("div", "tekst", item.tekst));
    if (item.merknad) a.append($("div", "merknad", `Forslag: ${item.forslag} – ${item.merknad}`));
    const valg = $("div", "valg");
    for (const k of Object.keys(DATA.regler)) {
      const b = $("button", k === "ok" ? "ok" : "", k);
      b.setAttribute("aria-pressed", String(fasit(item) === k && !!DATA.fasit[item.id]));
      if (!DATA.fasit[item.id] && item.forslag === k) b.style.borderStyle = "dashed";
      b.onclick = () => lagre(item, k);
      valg.append(b);
    }
    a.append(valg);
    const mod = Object.entries(DATA.modeller).filter(([, m]) => m[item.id]);
    if (mod.length) {
      const d = $("div", "modeller", "Modellene: ");
      for (const [navn, m] of mod) {
        const r = m[item.id], s = r.feil ? "feil" : r.regel + (r.sannsynligheter ? ` ${Math.round(100 * r.sannsynligheter[r.regel])} %` : "");
        d.append($("span", r.regel && r.regel !== fasit(item) ? "uenig" : "", `${navn}: ${s}  `));
      }
      a.append(d);
    }
    liste.append(a);
  }
}
async function lagre(item, regel) {
  const r = await fetch("/lagre", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: item.id, regel }) });
  if (r.ok) { DATA.fasit[item.id] = await r.json(); tegn(); }
}
for (const b of document.querySelectorAll(".filter button")) b.onclick = () => {
  filter = b.dataset.f; for (const x of document.querySelectorAll(".filter button")) x.setAttribute("aria-pressed", String(x === b)); tegn();
};
tegn();
</script>
</body>
</html>
"""
