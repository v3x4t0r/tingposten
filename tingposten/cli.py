import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from . import ark, config, innhenter, pdf, publiserer
from .bk360 import Blocked

_ADMIN_PASSORD = "TINGPOSTEN_ADMIN_PASSORD"  # admin.PASSORD_ENV, without importing the panel for every command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tingposten")
    sub = parser.add_subparsers(dest="cmd", required=True)

    hent = sub.add_parser("hent", help="fetch agenda items and log changes")
    hent.add_argument("--year", type=int, action="append", help="default: this year (+ last year in Jan/Feb)")
    hent.add_argument("--utvalg", action="append", help="utvalgId; default: Tier 1 organs")

    publiser = sub.add_parser("publiser", help="put site/ online (config.PUBLISER_TIL: statichost or github)")
    publiser.add_argument("--placeholder", action="store_true", help="build the 'kommer snart' page first")

    urler = sub.add_parser("pdf-urler", help="build PDF download URLs for a meeting (fetches nothing)")
    urler.add_argument("--utvalg", help="utvalgId, e.g. 571424 (required without --mangler)")
    urler.add_argument("--mangler", action="store_true",
                       help="every PDF the pipeline still needs, across meetings (with --utvalg/--dato: only those)")
    urler.add_argument("--fra", help="with --mangler: oldest meeting date (default: config.TILBAKE_DAGER back)")
    urler.add_argument("--dato", help="meeting date YYYY-MM-DD; default: the newest stored meeting")
    urler.add_argument("--all", action="store_true", help="include noise (innkalling, spørsmål, …)")
    urler.add_argument("--script", type=Path, help="also write a polite wget script to this path")

    arkp = sub.add_parser("ark", help="worksheet (CSV) for sorting a downloaded meeting by hand")
    arkp.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    arkp.add_argument("--dato", required=True, help="meeting date YYYY-MM-DD")
    arkp.add_argument("--out", type=Path, help="default: data/bergen/ark/<utvalg>-<dato>.csv")

    scorep = sub.add_parser("score", help="rank a downloaded meeting by impact score")
    scorep.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    scorep.add_argument("--dato", required=True, help="meeting date YYYY-MM-DD")
    scorep.add_argument("--sammenlign", action="store_true", help="compare with the hand ratings from `vurder`")

    sub.add_parser("rediger", help="delete-only editor: fact-checked drafts → publishable cards")

    lastp = sub.add_parser("last-ned", help="download missing PDFs (only when config.ALLOW_PDF_FETCH is on)")
    lastp.add_argument("--utvalg", help="only this utvalgId")
    lastp.add_argument("--dato", help="only this meeting date YYYY-MM-DD")

    kjorp = sub.add_parser("kjor", help="write, fact-check, classify and edit every decided sak with PDFs on disk")
    kjorp.add_argument("--utvalg", help="only this utvalgId")
    kjorp.add_argument("--dato", help="only this meeting date YYYY-MM-DD")
    kjorp.add_argument("--steg", help="comma-separated subset of skriv,sjekk,klassifiser,rediger,artikkel,"
                                      "artikkelsjekk,artikkelrediger")
    kjorp.add_argument("--parallel", type=int, default=3)
    kjorp.add_argument("--fra", help="oldest meeting date YYYY-MM-DD (default: config.TILBAKE_DAGER back)")

    byggp = sub.add_parser("bygg", help="build the static site into site/")
    byggp.add_argument("--idag", type=date.fromisoformat, help="pretend today is YYYY-MM-DD (for previews)")
    byggp.add_argument("--til", type=Path, help="build into this directory instead of site/")

    innp = sub.add_parser(
        "innlegg", help="publiser et debattinnlegg fra en leser (bygg og publiser etterpå)",
        description="Lagrer et debattinnlegg i data/bergen/innlegg/. Det vises på Debatt-siden og får en egen side "
                    "med kommentarfelt ved neste bygg. Teksten er skribentens egen, slik redaktøren har godkjent den.")
    innp.add_argument("--tittel", required=True)
    innp.add_argument("--forfatter", required=True, help="fullt navn (fornavn og etternavn)")
    innp.add_argument("--om", default="", help="hvem skribenten er, f.eks. «lærer, bor i Åsane» (valgfritt)")
    innp.add_argument("--fil", type=Path, required=True, help="teksten, avsnitt skilt med en tom linje")
    innp.add_argument("--sak", help="saken innlegget svarer på (nøkkel, f.eks. 900102/2026-10-29/1187-26)")
    innp.add_argument("--dato", type=date.fromisoformat, help="publiseringsdato YYYY-MM-DD (standard: i dag)")

    rettp = sub.add_parser(
        "rett", help="legg til en synlig rettelse på en sak (bygger og publiserer ikke)",
        description="Legger til en rettelse med dagens dato i data/bergen/rettelser/. Saken får en merknad om "
                    "hva som ble endret og når. Rettelsen brukes når siden bygges, og overlever at pipelinen "
                    "skriver kortet på nytt. Kjør `tingposten bygg` og `tingposten publiser` etterpå.")
    rettp.add_argument("sak", help="sakens adresse eller nøkkel: https://tingposten.no/bergen/sak/571424/2026-09-23/"
                                   "266-26/, bergen/sak/571424/2026-09-23/266-26 eller 571424/2026-09-23/266-26")
    rettp.add_argument("--merknad", required=True,
                       help="hva som var feil og hva som er riktig, én eller to setninger. Vises på saken som du skriver den")
    rettp.add_argument("--felt", help="det som rettes: overskrift, ingress, hva_endres, hvem_pavirkes, hvorfor, "
                                      "status, nar_og_hvor eller artikkel:<n> (avsnitt n i brødteksten). "
                                      "Uten --felt er rettelsen bare en merknad")
    rettp.add_argument("--tekst", help="ny tekst for feltet; --tekst '' fjerner teksten")
    rettp.add_argument("--kilde", action="append", metavar="DOK:SIDE",
                       help="kilde for den nye teksten, f.eks. saksframlegg:5 (kan gjentas). Uten beholder feltet "
                            "sine kilder")

    utg = sub.add_parser(
        "utgave", help="papirutgave av ett møte som PDF (trenger: uv run --extra utgave)",
        description="Lager en trykkeklar PDF av ett møte: forside, hovedsakene i sin helhet, de andre sakene kort, "
                    "kilder, kortadresse og QR-kode til hver sak. Bare faktasjekket innhold, som på nettsiden. "
                    "Kortadressene (/s/…) virker når nettsiden er bygget og publisert på nytt.")
    utg.add_argument("--mote", required=True, help="møtet: <utvalgId>-<dato>, f.eks. 571424-2026-09-23")
    utg.add_argument("--format", default="tabloid", choices=("tabloid", "a4", "a5"),
                     help="tabloid: 280 × 400 mm, 5 spalter; a4: A4-hefte, 3 spalter; a5: A5-hefte, 2 spalter "
                          "(skriver også <fil>-hefte.pdf: A4-ark med to sider side om side, klar for hjemmeskriver)")
    utg.add_argument("--til", type=Path, required=True, help="PDF-filen som skal skrives")
    utg.add_argument("--png", type=Path, help="skriv også en PNG av hver side i denne mappen")
    utg.add_argument("--dpi", type=int, default=110, help="oppløsning for --png (standard 110)")

    adm = sub.add_parser(
        "admin", help="kontrollpanel for Pi-en: status, neste protokoll og en knapp som laster ned og publiserer",
        description="Et lite nettpanel: hvordan siden står, hvilke møter som venter på protokoll og når den er "
                    "ventet, og knapper som starter deploy/run-daily.sh (--manuell: last ned PDF-er, skriv, bygg og "
                    "publiser; --sjekk: bare hent agendaen). Ingenting hentes eller publiseres før en knapp trykkes.")
    adm.add_argument("--vert", default="127.0.0.1",
                     help=f"adressen panelet lytter på; 0.0.0.0 = hele hjemmenettet (krever {_ADMIN_PASSORD} i .env)")
    adm.add_argument("--port", type=int, default=8787)

    vurd = sub.add_parser("vurder", help="local web page for sorting a meeting's draft cards")
    vurd.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    vurd.add_argument("--dato", required=True, help="meeting date YYYY-MM-DD")
    vurd.add_argument("--port", type=int, default=8765)
    vurd.add_argument("--apne", action="store_true", help="open the page in the browser")

    dtest = sub.add_parser(
        "debatt-test", help="kjør testkommentarene gjennom en moderator og vis hvor godt den treffer",
        description="Kjører data/debatt/testsett.jsonl gjennom moderatoren og sammenligner med fasiten "
                    "(data/debatt/fasit.json; forslagene teller der fasiten ikke er satt ennå). Skriver én linje "
                    "per kommentar til data/debatt/test/ og en tabell: de innstilte grensene (konfig) og én felles "
                    "terskel for alle regler til sammenligning. Med --rapport kjøres ingenting; "
                    "tabellen lages fra lagrede kjøringer.")
    dtest.add_argument("--moderator", action="append", choices=("hailo", "cpu", "laya", "jev", "claude"),
                       help="kan gjentas; hver kjøres for seg")
    dtest.add_argument("--bare", type=int, help="bare de første N kommentarene (prøvekjøring)")
    dtest.add_argument("--rapport", nargs="*", type=Path, metavar="KJØRING",
                       help="lag tabellen fra disse kjøringene (uten filer: den nyeste per moderator)")
    dtest.add_argument("--stol-pa-etikett", action="store_true",
                       help="regn et svar uten sannsynligheter (hailo) som sikkert")

    dsrv = sub.add_parser(
        "debatt", help="kommentarserveren (trenger: uv run --extra debatt)",
        description="Starter kommentarserveren: innlogging med e-postlenke, kommentarer, stemmer og moderering. "
                    "Lytter bare lokalt; Tailscale Funnel gjør den nåbar for leserne. Innstillinger i "
                    "miljøet (DEBATT_SMTP_*, DEBATT_AVSENDER, TINGPOSTEN_ANTHROPIC_API_KEY, TYPESAFE_API_KEY).")
    dsrv.add_argument("--vert", default="127.0.0.1")
    dsrv.add_argument("--port", type=int, default=8790)

    dlok = sub.add_parser(
        "debatt-lokal", help="prøv kommentarfeltet på egen maskin (trenger: uv run --extra debatt)",
        description="Bygger siden med kommentarer på, viser den på 127.0.0.1 og kjører kommentarserveren ved "
                    "siden av med den ekte moderatoren (Jev trenger TYPESAFE_API_KEY i .env). Innloggingslenkene "
                    "skrives i terminalen. Alt ligger i data/debatt/lokal/; ingenting publiseres.")
    dlok.add_argument("--port", type=int, default=8091, help="siden (standard 8091)")
    dlok.add_argument("--api-port", type=int, default=8790, help="kommentarserveren (standard 8790)")
    dlok.add_argument("--moderator", action="append", choices=("hailo", "cpu", "laya", "jev", "claude"),
                      help="i stedet for config.DEBATT_MODERATOR (kan gjentas)")

    dfas = sub.add_parser("debatt-fasit", help="lokal side for å sette fasit på testkommentarene")
    dfas.add_argument("--port", type=int, default=8766)
    dfas.add_argument("--apne", action="store_true", help="åpne siden i nettleseren")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "hent":
        years = args.year or innhenter.years_to_fetch(date.today())
        try:
            innhenter.run(years, args.utvalg or list(config.TIER1))
        except Blocked as e:
            logging.error("giving up this run, server keeps refusing: %s", e)
            return 2
    elif args.cmd == "publiser":
        if args.placeholder:
            publiserer.build_placeholder()
        try:
            publiserer.publiser(message=f"Publiser {datetime.now():%Y-%m-%d %H:%M}")
        except RuntimeError as e:
            logging.error("%s", e)
            return 1
    elif args.cmd == "pdf-urler":
        return _pdf_urler(args)
    elif args.cmd == "ark":
        return _ark(args)
    elif args.cmd == "vurder":
        return _vurder(args)
    elif args.cmd == "admin":
        return _admin(args)
    elif args.cmd == "debatt":
        import uvicorn

        from .debatt import server

        uvicorn.run(server.app_fra_config(), host=args.vert, port=args.port, proxy_headers=False,
                    server_header=False, log_level="info")
    elif args.cmd == "debatt-lokal":
        from .debatt import lokal

        try:
            lokal.kjor(args.port, args.api_port, tuple(args.moderator) if args.moderator else None)
        except KeyboardInterrupt:
            pass
    elif args.cmd == "debatt-test":
        return _debatt_test(args)
    elif args.cmd == "debatt-fasit":
        return _debatt_fasit(args)
    elif args.cmd == "score":
        return _score(args)
    elif args.cmd == "rett":
        return _rett(args)
    elif args.cmd == "innlegg":
        from . import innlegg

        tekst = args.fil.read_text(encoding="utf-8")
        path = innlegg.ny(args.tittel, args.forfatter, tekst, om=args.om, dato=args.dato, sak=args.sak)
        if len(tekst) > innlegg.MAKS_TEGN:
            logging.warning("innlegget har %d tegn (siden ber om høyst %d)", len(tekst), innlegg.MAKS_TEGN)
        logging.info("lagret %s; bygg og publiser for å vise det", path)
    elif args.cmd == "rediger":
        from . import redaktor

        items = [i for uid in config.TIER1 for i in (_load_items(uid) or [])]
        logging.info("redaktør: %s", redaktor.run(items))
    elif args.cmd == "last-ned":
        from . import nedlaster, pipeline

        counts = nedlaster.last_ned(pipeline.saker(config.DATA_DIR, args.utvalg, args.dato))
        logging.info("last-ned: %s", counts)
        return 2 if counts.get("blokkert") else 0
    elif args.cmd == "kjor":
        from . import pipeline

        steg = tuple(args.steg.split(",")) if args.steg else pipeline.STEG
        if unknown := set(steg) - set(pipeline.STEG):
            parser.error(f"unknown steg: {', '.join(sorted(unknown))}")
        pipeline.kjor(utvalg=args.utvalg, dato=args.dato, steg=steg, parallel=args.parallel, fra=args.fra)
    elif args.cmd == "utgave":
        from . import utgave

        try:
            info = utgave.lag(args.mote, args.format, args.til, forhandsvisning=args.png, dpi=args.dpi)
        except utgave.UtgaveFeil as e:
            logging.error("utgaven ble ikke laget: %s", e)
            return 1
        logging.info("utgave: %s, %d sider, %d hele saker, %d korte → %s", info["format"], info["sider"],
                     len(info["hele"]), len(info["korte"]), info["pdf"])
        if info.get("hefte"):
            logging.info("hefte for utskrift: %d A4-ark, tosidig med vending på kortsiden → %s", info["ark"],
                         info["hefte"])
    elif args.cmd == "bygg":
        from . import bygg

        logging.info("bygg: %s", bygg.build(args.til or config.SITE_DIR, idag=args.idag))
    return 0


def _rett(args) -> int:
    from . import rettelser

    try:
        key = rettelser.sak_key(args.sak)
        kilder = [rettelser.kilde(k) for k in args.kilde or []]
        entry = rettelser.ny(config.DATA_DIR, key, args.merknad, felt=args.felt, tekst=args.tekst, kilder=kilder)
    except ValueError as e:
        logging.error("rettelsen ble ikke lagret: %s", e)
        return 1
    print(json.dumps(entry, ensure_ascii=False, indent=1))
    print(f"Lagret i {rettelser.sti(config.DATA_DIR, key)}. Vises når siden bygges: "
          "tingposten bygg && tingposten publiser")
    return 0


def _score(args) -> int:
    from . import score, vurder

    items = _load_items(args.utvalg)
    if items is None:
        return 1
    ranked = score.rank([i for i in items if i.get("moteDato") == args.dato], config.DATA_DIR / "pdf")
    if not args.sammenlign:
        for r in ranked:
            print(f"{r['score']:3}  {r['nr']:7} {' · '.join(r['grunner']):55} {r['tittel'][:60]}")
        return 0
    ratings_file = vurder.ratings_path(args.utvalg, args.dato)
    if not ratings_file.exists():
        logging.error("no ratings yet in %s — run `tingposten vurder` first", ratings_file)
        return 1
    print(json.dumps(score.compare(ranked, json.loads(ratings_file.read_text())), ensure_ascii=False, indent=1))
    return 0


def _vurder(args) -> int:
    from . import vurder

    items = _load_items(args.utvalg)
    if items is None:
        return 1
    server = vurder.make_server(args.utvalg, args.dato, items, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logging.info("vurdering: %s (saves to %s) — Ctrl+C to stop", url, vurder.ratings_path(args.utvalg, args.dato))
    if args.apne:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _admin(args) -> int:
    from . import admin

    try:
        server = admin.make_server(args.vert, args.port, os.environ.get(_ADMIN_PASSORD) or None)
    except ValueError as e:
        logging.error("%s", e)
        return 1
    import socket

    host = socket.gethostname() if args.vert in ("0.0.0.0", "::") else args.vert
    logging.info("kontrollpanel: http://%s:%d/ — Ctrl+C to stop", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _debatt_test(args) -> int:
    from .debatt import moderering, testsett

    try:
        sett = testsett.les_sett()
    except FileNotFoundError as e:
        logging.error("%s", e)
        return 1
    fasit = testsett.les_fasit()
    if args.rapport is not None:
        filer = args.rapport or list(testsett.nyeste_kjoringer().values())
    else:
        if not args.moderator:
            logging.error("velg minst én --moderator (hailo, cpu, jev, claude), eller --rapport")
            return 1
        filer = []
        for navn in args.moderator:
            m = moderering.backend(navn)
            logging.info("debatt-test: %d kommentarer gjennom %s", min(len(sett), args.bare or len(sett)), m.name)
            filer.append(testsett.kjor(m, sett, bare=args.bare))
    rapporter = {}
    for f in filer:
        r = testsett.rapport(testsett.les_resultat(f), sett, fasit, args.stol_pa_etikett)
        rapporter[r["modell"] or f.stem] = r
        logging.info("%s: %d svar, %d feil, %d av %d med fasit satt av redaktøren", f.name, r["n"] - r["feil"],
                     r["feil"], r["kontrollert"], r["n"])
    print(testsett.tabell(rapporter))
    return 0


def _debatt_fasit(args) -> int:
    from .debatt import testsett

    if not testsett.sett_sti().exists():
        logging.error("no test set at %s", testsett.sett_sti())
        return 1
    server = testsett.make_server(args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logging.info("fasit: %s (lagres i %s) — Ctrl+C for å stoppe", url, testsett.fasit_sti())
    if args.apne:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _load_items(utvalg: str) -> list[dict] | None:
    store_path = config.DATA_DIR / "agendapunkter" / f"{utvalg}.json"
    if not store_path.exists():
        logging.error("no stored data for utvalg %s — run `tingposten hent` first", utvalg)
        return None
    return list(json.loads(store_path.read_text()).values())


def _ark(args) -> int:
    items = _load_items(args.utvalg)
    if items is None:
        return 1
    meeting = [i for i in items if i.get("moteDato") == args.dato]
    pdf_dir = config.DATA_DIR / "pdf"
    if not (pdf_dir / args.dato).is_dir():
        logging.error("no PDFs in %s — download them first (tingposten pdf-urler --script …)", pdf_dir / args.dato)
        return 1
    rows = ark.build(meeting, pdf_dir)
    out = args.out or ark.default_path(args.utvalg, args.dato)
    ark.write_csv(rows, out)
    logging.info("wrote %d saker to %s", len(rows), out)
    return 0


def _pdf_urler(args) -> int:
    if args.mangler:
        return _pdf_mangler(args)
    if not args.utvalg:
        logging.error("--utvalg is required (or use --mangler)")
        return 1
    items = _load_items(args.utvalg)
    if items is None:
        return 1
    dato = args.dato or max((i.get("moteDato") or "" for i in items), default="")
    if not dato:
        logging.error("store for utvalg %s is empty", args.utvalg)
        return 1
    meeting = [i for i in items if i.get("moteDato") == dato]
    targets = pdf.targets(meeting, drop_noise=not args.all)
    if not targets:
        logging.error("no PDFs for utvalg %s on %s (noise filtered? try --all)", args.utvalg, dato)
        return 1
    logging.info("%d PDF(s) for utvalg %s, meeting %s", len(targets), args.utvalg, dato)
    for t in targets:
        print(f"{t['url']}\t{t['filename']}")
    if args.script:
        args.script.write_text(pdf.build_script(targets))
        args.script.chmod(0o755)
        logging.info("wrote download script to %s (run it yourself when unblocked)", args.script)
    return 0



def _pdf_mangler(args) -> int:
    """One polite script for every saksframlegg/protokoll the pipeline lacks.
    Writing it is not fetching: the user decides when to run it."""
    from . import pipeline

    fra = None if args.dato else (args.fra or pipeline.fra_dato())
    items = pipeline.saker(config.DATA_DIR, args.utvalg, args.dato, fra)
    # Protokoller published before the meeting's decision was added are fetched again.
    targets = pipeline.pdf_som_mangler(items, config.DATA_DIR)
    if oppdater := sum(1 for t in targets if t.get("oppdater")):
        logging.info("%d protokoll(er) mangler møtets vedtak og hentes på nytt", oppdater)
    meetings = sorted({t["filename"].rsplit("/", 1)[0] for t in targets})
    logging.info("%d PDF(s) to fetch across %d meeting(s)%s", len(targets), len(meetings), f" since {fra}" if fra else "")
    for t in targets:
        print(f"{t['url']}\t{t['filename']}")
    if args.script:
        if targets:
            args.script.parent.mkdir(parents=True, exist_ok=True)
            args.script.write_text(pdf.build_script(targets, dest=str(config.DATA_DIR / "pdf")))
            args.script.chmod(0o755)
            minutes = round(len(targets) * 6 / 60)
            logging.info("wrote %s (about %d min at the polite pace) — run it yourself: bash %s",
                         args.script, minutes, args.script)
        elif args.script.exists():
            args.script.unlink()  # a stale script would fetch nothing anyway; don't leave it lying around
    return 0


if __name__ == "__main__":
    sys.exit(main())
