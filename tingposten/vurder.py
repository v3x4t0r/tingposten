"""Vurdering: a local web page for sorting a meeting's draft cards by hand.

Serves the draft cards (data/bergen/kort/<utvalgId>/<dato>/) on 127.0.0.1 with buttons
for forside / kort / støy, a 1–5 rating and a comment. Every click is saved
to data/bergen/vurdering/<utvalg>-<dato>.json. Nothing leaves the machine:
the source PDFs are served from the local copy too. Fact-check results in
data/bergen/sjekk/<utvalgId>/<dato>/ are shown when present.
"""

import html
import json
import logging
import os
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, pdf

log = logging.getLogger(__name__)

FIELDS = (
    ("ingress", "Ingress"),
    ("hva_endres", "Hva endres"),
    ("hvem_pavirkes", "Hvem påvirkes"),
    ("hvorfor", "Hvorfor"),
    ("status", "Status"),
    ("nar_og_hvor", "Når og hvor"),
)
PILES = ("forside", "kort", "stoy")
MONTHS = ("januar", "februar", "mars", "april", "mai", "juni", "juli",
          "august", "september", "oktober", "november", "desember")


def load_cards(items: list[dict], dato: str, data_dir: Path = config.DATA_DIR) -> list[dict]:
    """Draft cards for one meeting joined with metadata, in agenda order."""
    cards = []
    for item in sorted(items, key=lambda i: i.get("seq") or 0):
        if item.get("moteDato") != dato or pdf.is_noise(item):
            continue
        draft = _read(data_dir / "kort" / f"{pdf.key(item)}.json")
        check = _read(data_dir / "sjekk" / f"{pdf.key(item)}.json")
        links = {k: f"/pdf/{pdf.filename(item, k)}" for k, _, _ in pdf.KINDS if pdf.url(item, k)}
        links |= {f"vedlegg-{v['nr']}": f"/pdf/{pdf.vedlegg_fil(item, v['nr'])}" for v in pdf.vedlegg_liste(item, data_dir) or []}
        checks = check.get("setninger", [])
        cards.append({
            "nr": item.get("referansenr") or pdf.tag(item),
            "tittel": item.get("tittel", ""),
            "kategori": item.get("kategori", ""),
            "overskrift": draft.get("overskrift", ""),
            "felt": [
                {
                    "label": label,
                    "deler": _parts(draft[key], [c for c in checks if c.get("felt") == key]),
                    "kilder": [
                        {"dok": k["dok"], "side": k["side"], "href": f"{links[k['dok']]}#page={k['side']}"}
                        for k in draft.get("kilder", [])
                        if k.get("felt") == key and k.get("dok") in links
                    ],
                }
                for key, label in FIELDS if draft.get(key)
            ],
            "merknad": draft.get("merknad", ""),
            "sjekket": bool(check),
            "uten_stotte": sum(1 for c in checks if c.get("stottet") is False),
            "personvern": check.get("personvern", []),
            "pdf": links,
            "mangler": not draft,
        })
    return cards


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _parts(text: str, checks: list[dict]) -> list[dict]:
    """Split a field into checked sentences and unchecked rest, in order."""
    parts, pos = [], 0
    for c in checks:
        i = text.find(c.get("setning", ""), pos)
        if not c.get("setning") or i < 0:
            continue
        if i > pos:
            parts.append({"tekst": text[pos:i], "stottet": None})
        parts.append({"tekst": c["setning"], "stottet": c.get("stottet"), "begrunnelse": c.get("begrunnelse", "")})
        pos = i + len(c["setning"])
    if pos < len(text):
        parts.append({"tekst": text[pos:], "stottet": None})
    return parts


def clean(body: dict, known: set[str]) -> tuple[str, dict]:
    """Validate a rating sent by the page."""
    nr = body.get("nr")
    if nr not in known:
        raise ValueError("ukjent sak")
    vurdering = body.get("vurdering")
    if vurdering not in (*PILES, None):
        raise ValueError("ugyldig vurdering")
    merkbar = body.get("merkbar")
    if merkbar is not None and (type(merkbar) is not int or not 1 <= merkbar <= 5):
        raise ValueError("merkbar må være 1–5")
    kommentar = body.get("kommentar") or ""
    if not isinstance(kommentar, str) or len(kommentar) > 2000:
        raise ValueError("ugyldig kommentar")
    return nr, {"vurdering": vurdering, "merkbar": merkbar, "kommentar": kommentar.strip()}


def ratings_path(utvalg: str, dato: str, data_dir: Path = config.DATA_DIR) -> Path:
    return data_dir / "vurdering" / f"{utvalg}-{dato}.json"


def nb_date(dato: str) -> str:
    y, m, d = (int(x) for x in dato.split("-"))
    return f"{d}. {MONTHS[m - 1]} {y}"


def page(title: str, cards: list[dict], ratings: dict) -> str:
    data = json.dumps({"kort": cards, "vurdering": ratings}, ensure_ascii=False)
    # The data sits inside <script>; "</" must not close it.
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", data.replace("</", "<\\/"))


def make_server(utvalg: str, dato: str, items: list[dict], port: int = 8765,
                data_dir: Path = config.DATA_DIR) -> ThreadingHTTPServer:
    title = f"{config.TIER1.get(utvalg, utvalg)} · {nb_date(dato)}"
    store = ratings_path(utvalg, dato, data_dir)
    pdf_dir = (data_dir / "pdf").resolve()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if path == "/":
                # Re-read on every load so new drafts and checks show up on refresh.
                body = page(title, load_cards(items, dato, data_dir), _read(store))
                return self._send(200, "text/html; charset=utf-8", body.encode())
            if path.startswith("/pdf/"):
                target = (pdf_dir / path[len("/pdf/"):]).resolve()
                if target.is_relative_to(pdf_dir) and target.suffix == ".pdf" and target.is_file():
                    return self._send(200, "application/pdf", target.read_bytes())
            self._send(404, "text/plain; charset=utf-8", b"Ikke funnet")

        def do_POST(self):
            if self.path != "/lagre":
                return self._send(404, "text/plain; charset=utf-8", b"Ikke funnet")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 10_000:
                    raise ValueError("for stor")
                known = {c["nr"] for c in load_cards(items, dato, data_dir)}
                nr, entry = clean(json.loads(self.rfile.read(length)), known)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"feil": str(e)})
            with lock:
                ratings = _read(store)
                ratings[nr] = {**entry, "tid": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                store.parent.mkdir(parents=True, exist_ok=True)
                tmp = store.with_suffix(".tmp")
                tmp.write_text(json.dumps(ratings, ensure_ascii=False, indent=1))
                os.replace(tmp, store)
            self._json(200, {"ok": True})

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, "application/json", json.dumps(obj, ensure_ascii=False).encode())

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


PAGE = r"""<!doctype html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vurdering · __TITLE__</title>
<style>
:root {
  --bg: #f6f5f1; --card: #ffffff; --fg: #1d1d1b; --muted: #66655f; --line: #e0ded6;
  --accent: #1f5f8b; --accent-bg: #e4eef6;
  --yes: #2b7a4b; --yes-bg: #e3f2e8; --no: #9a3b2e; --no-bg: #f6e6e2;
  --warn: #8a5a00; --warn-bg: #fdf0d2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141413; --card: #1e1e1c; --fg: #ecebe6; --muted: #a3a29b; --line: #34332f;
    --accent: #7fb5dd; --accent-bg: #1b2b37;
    --yes: #7ccb9b; --yes-bg: #18291f; --no: #e5927f; --no-bg: #331e1a;
    --warn: #f0c062; --warn-bg: #33280f;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); }
header { position: sticky; top: 0; z-index: 2; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 10px 16px; }
.bar { max-width: 46rem; margin: 0 auto; display: flex; flex-wrap: wrap;
  gap: 8px 16px; align-items: center; justify-content: space-between; }
h1 { font-size: 1rem; margin: 0; }
.sub { color: var(--muted); font-size: .85rem; }
.filters { display: flex; flex-wrap: wrap; gap: 4px; }
.filters button { font: inherit; font-size: .8rem; border: 1px solid var(--line);
  background: var(--card); color: var(--fg); border-radius: 99px; padding: 3px 10px; cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--fg); color: var(--bg); border-color: var(--fg); }
main { max-width: 46rem; margin: 0 auto; padding: 16px; }
.intro { color: var(--muted); font-size: .9rem; margin: 0 0 16px; }
article { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; margin: 0 0 16px; scroll-margin-top: 96px; transition: opacity .15s; }
article:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
article.forside { border-color: var(--yes); box-shadow: inset 4px 0 0 var(--yes); }
article.stoy { opacity: .5; }
article.stoy h2 { text-decoration: line-through; }
.meta { color: var(--muted); font-size: .82rem; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chip { font-size: .72rem; border: 1px solid var(--line); border-radius: 99px; padding: 0 8px; }
.chip.ok { color: var(--yes); border-color: var(--yes); }
.chip.warn { color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }
h2 { font-size: 1.2rem; margin: 6px 0 2px; line-height: 1.3; }
.orig { color: var(--muted); font-size: .85rem; margin: 0 0 8px; }
dt { font-weight: 600; font-size: .82rem; margin-top: 10px; color: var(--muted); }
dd { margin: 2px 0 0; }
.unsupported { background: var(--warn-bg); text-decoration: underline wavy var(--warn); cursor: help; }
.src { font-size: .75rem; margin-left: 6px; white-space: nowrap; }
.docs { margin-top: 10px; }
details { margin-top: 10px; font-size: .85rem; color: var(--muted); }
details p { margin: 4px 0 0; }
.actions { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); display: grid; gap: 10px; }
.row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.label { font-size: .82rem; color: var(--muted); min-width: 11rem; }
button.pile, button.num { font: inherit; font-size: .9rem; border: 1px solid var(--line);
  background: var(--bg); color: var(--fg); border-radius: 8px; padding: 6px 14px;
  cursor: pointer; min-height: 38px; }
button.num { min-width: 42px; padding: 6px 0; }
button.pile-forside[aria-pressed="true"] { background: var(--yes-bg); border-color: var(--yes); color: var(--yes); font-weight: 600; }
button.pile-kort[aria-pressed="true"] { background: var(--accent-bg); border-color: var(--accent); color: var(--accent); font-weight: 600; }
button.pile-stoy[aria-pressed="true"] { background: var(--no-bg); border-color: var(--no); color: var(--no); font-weight: 600; }
button.num[aria-pressed="true"] { background: var(--fg); color: var(--bg); border-color: var(--fg); }
textarea { width: 100%; font: inherit; font-size: .9rem; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--fg); padding: 8px 10px; min-height: 2.6em; resize: vertical; }
.status { font-size: .78rem; color: var(--muted); min-height: 1.1em; }
.help { max-width: 46rem; margin: 0 auto 32px; padding: 0 16px; color: var(--muted); font-size: .8rem; }
kbd { font: .75rem ui-monospace, monospace; border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; background: var(--card); }
.done { background: var(--yes-bg); color: var(--yes); border-radius: 10px; padding: 12px 16px; margin: 0 0 16px; }
@media (max-width: 560px) { .label { min-width: 100%; } }
</style>
</head>
<body>
<header><div class="bar">
  <div><h1>Tingposten · vurdering</h1><div class="sub" id="progress"></div></div>
  <div class="filters" id="filters" role="group" aria-label="Vis">
    <button type="button" data-filter="alle" aria-pressed="true">Alle</button>
    <button type="button" data-filter="ikke" aria-pressed="false">Ikke vurdert</button>
    <button type="button" data-filter="forside" aria-pressed="false">Forside</button>
    <button type="button" data-filter="kort" aria-pressed="false">Kort</button>
    <button type="button" data-filter="stoy" aria-pressed="false">Støy</button>
  </div>
</div></header>
<main>
  <p class="intro"><strong>__TITLE__.</strong> Kortene er maskinskrevne utkast fra saksframlegg og protokoll.
  Vurder hver sak slik en vanlig innbygger ville lest den. Alt lagres med en gang på denne maskinen.</p>
  <div id="done" class="done" hidden>Alle sakene er vurdert. Si fra til Claude i terminalen.</div>
  <div id="list"></div>
</main>
<p class="help">Tastatur: <kbd>f</kbd> forside · <kbd>k</kbd> kort · <kbd>s</kbd> støy ·
<kbd>1</kbd>–<kbd>5</kbd> merker innbyggere det · <kbd>n</kbd> eller <kbd>Enter</kbd> neste · <kbd>p</kbd> forrige.
Klikk på samme valg igjen for å angre.</p>
<script>
const DATA = __DATA__;
const state = DATA.vurdering || {};
const PILES = [["forside", "Forside"], ["kort", "Kort"], ["stoy", "Støy"]];
const SCALE = {1: "Ingen merker det", 2: "Noen få merker det", 3: "En gruppe eller et område merker det",
               4: "Mange merker det", 5: "Nesten alle merker det"};
const list = document.getElementById("list");
let focusNr = null;

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function card(c) {
  const s = state[c.nr] || {};
  const a = el("article", {tabindex: "-1", "data-nr": c.nr, onfocusin: () => { focusNr = c.nr; }});
  const meta = el("div", {class: "meta"}, `${c.nr} · ${c.kategori}`);
  if (c.mangler) meta.append(el("span", {class: "chip warn"}, "kort mangler"));
  else meta.append(el("span", {class: "chip"}, "utkast"));
  if (c.sjekket) meta.append(c.uten_stotte
    ? el("span", {class: "chip warn"}, `${c.uten_stotte} setning${c.uten_stotte > 1 ? "er" : ""} uten støtte i kilden`)
    : el("span", {class: "chip ok"}, "faktasjekket"));
  if (c.personvern && c.personvern.length) meta.append(el("span", {class: "chip warn"}, "personvern?"));
  a.append(meta, el("h2", {}, c.overskrift || c.tittel));
  if (c.overskrift) a.append(el("p", {class: "orig"}, c.tittel));
  const dl = el("dl");
  for (const f of c.felt) {
    const dd = el("dd");
    for (const p of f.deler) {
      dd.append(p.stottet === false
        ? el("span", {class: "unsupported", title: "Faktasjekk: " + (p.begrunnelse || "ikke dekket av kilden")}, p.tekst)
        : p.tekst);
    }
    for (const k of f.kilder) dd.append(el("a", {class: "src", href: k.href, target: "_blank", rel: "noopener"}, `${k.dok} s. ${k.side}`));
    dl.append(el("dt", {}, f.label), dd);
  }
  a.append(dl);
  const docs = el("div", {class: "meta docs"}, "Dokumenter:");
  for (const [k, href] of Object.entries(c.pdf)) docs.append(el("a", {href, target: "_blank", rel: "noopener"}, k));
  a.append(docs);
  if (c.merknad) a.append(el("details", {}, el("summary", {}, "Merknad fra skribenten"), el("p", {}, c.merknad)));
  if (c.personvern && c.personvern.length)
    a.append(el("details", {open: ""}, el("summary", {}, "Personvern"), ...c.personvern.map(t => el("p", {}, t))));

  const piles = el("div", {class: "row"}, el("span", {class: "label"}, "Vurdering"));
  for (const [key, label] of PILES)
    piles.append(el("button", {type: "button", class: `pile pile-${key}`, "data-pile": key,
      onclick: () => update(c.nr, {vurdering: (state[c.nr] || {}).vurdering === key ? null : key})}, label));
  const nums = el("div", {class: "row"}, el("span", {class: "label"}, "Merker innbyggere det?"));
  for (let n = 1; n <= 5; n++)
    nums.append(el("button", {type: "button", class: "num", "data-num": String(n), title: SCALE[n],
      onclick: () => update(c.nr, {merkbar: (state[c.nr] || {}).merkbar === n ? null : n})}, String(n)));
  const ta = el("textarea", {placeholder: "Kommentar: hvorfor? (valgfritt)", "aria-label": "Kommentar", rows: "1"});
  ta.value = s.kommentar || "";
  ta.addEventListener("input", debounce(() => update(c.nr, {kommentar: ta.value}), 600));
  a.append(el("div", {class: "actions"}, piles, nums, ta, el("div", {class: "status", "aria-live": "polite"})));
  paint(a);
  return a;
}

function article(nr) { return list.querySelector(`article[data-nr="${CSS.escape(nr)}"]`); }

function paint(a) {
  const s = state[a.dataset.nr] || {};
  for (const [key] of PILES) a.classList.toggle(key, s.vurdering === key);
  a.querySelectorAll("button.pile").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.pile === s.vurdering)));
  a.querySelectorAll("button.num").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.num) === s.merkbar)));
}

async function update(nr, patch) {
  const s = state[nr] = {vurdering: null, merkbar: null, kommentar: "", ...state[nr], ...patch};
  const a = article(nr);
  paint(a);
  progress();
  const st = a.querySelector(".status");
  st.textContent = "Lagrer …";
  try {
    const r = await fetch("/lagre", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nr, vurdering: s.vurdering, merkbar: s.merkbar, kommentar: s.kommentar})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).feil || `HTTP ${r.status}`);
    st.textContent = "Lagret";
  } catch (e) {
    st.textContent = "Kunne ikke lagre: " + e.message;
  }
}

function progress() {
  const total = DATA.kort.length;
  const count = k => DATA.kort.filter(c => (state[c.nr] || {}).vurdering === k).length;
  const rated = DATA.kort.filter(c => (state[c.nr] || {}).vurdering).length;
  document.getElementById("progress").textContent =
    `${rated} av ${total} vurdert · forside ${count("forside")} · kort ${count("kort")} · støy ${count("stoy")}`;
  document.getElementById("done").hidden = rated < total;
}

function applyFilter(f) {
  document.querySelectorAll("#filters button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.filter === f)));
  list.querySelectorAll("article").forEach(a => {
    const v = (state[a.dataset.nr] || {}).vurdering;
    a.hidden = !(f === "alle" || (f === "ikke" ? !v : v === f));
  });
}
document.getElementById("filters").addEventListener("click", e => {
  if (e.target.dataset.filter) applyFilter(e.target.dataset.filter);
});

function visible() { return [...list.querySelectorAll("article:not([hidden])")]; }
function go(step) {
  const v = visible();
  if (!v.length) return;
  const i = v.findIndex(a => a.dataset.nr === focusNr);
  const next = v[Math.min(Math.max(i + step, 0), v.length - 1)] || v[0];
  next.focus({preventScroll: true});
  next.scrollIntoView({block: "start", behavior: "smooth"});
}

document.addEventListener("keydown", e => {
  if (e.target.matches("textarea, input") || e.metaKey || e.ctrlKey || e.altKey || !focusNr) return;
  const s = state[focusNr] || {};
  const pile = {f: "forside", k: "kort", s: "stoy"}[e.key];
  if (pile) update(focusNr, {vurdering: s.vurdering === pile ? null : pile});
  else if (/^[1-5]$/.test(e.key)) update(focusNr, {merkbar: s.merkbar === Number(e.key) ? null : Number(e.key)});
  else if (e.key === "n" || (e.key === "Enter" && !e.target.matches("button, a"))) { go(1); e.preventDefault(); }
  else if (e.key === "p") go(-1);
});

list.replaceChildren(...DATA.kort.map(card));
progress();
// Start at the first sak not yet rated; keys act on the outlined card.
const start = DATA.kort.find(c => !(state[c.nr] || {}).vurdering) || DATA.kort[0];
if (start) { focusNr = start.nr; article(start.nr).focus(); }
</script>
</body>
</html>
"""
