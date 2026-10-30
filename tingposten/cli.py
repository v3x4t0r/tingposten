import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import ark, config, innhenter, pdf, publiserer
from .bk360 import Blocked


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
                                      "artikkelsjekk,artikkelrediger,artikkeltittel")
    kjorp.add_argument("--parallel", type=int, default=3)
    kjorp.add_argument("--fra", help="oldest meeting date YYYY-MM-DD (default: config.TILBAKE_DAGER back)")

    byggp = sub.add_parser("bygg", help="build the static site into site/")
    byggp.add_argument("--idag", type=date.fromisoformat, help="pretend today is YYYY-MM-DD (for previews)")
    byggp.add_argument("--til", type=Path, help="build into this directory instead of site/")

    vurd = sub.add_parser("vurder", help="local web page for sorting a meeting's draft cards")
    vurd.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    vurd.add_argument("--dato", required=True, help="meeting date YYYY-MM-DD")
    vurd.add_argument("--port", type=int, default=8765)
    vurd.add_argument("--apne", action="store_true", help="open the page in the browser")

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
    elif args.cmd == "score":
        return _score(args)
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
    elif args.cmd == "bygg":
        from . import bygg

        logging.info("bygg: %s", bygg.build(args.til or config.SITE_DIR, idag=args.idag))
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
    targets = [t for t in pdf.targets(items) if not (config.DATA_DIR / "pdf" / t["filename"]).exists()]
    # Protokoller published before the meeting's decision was added: fetch again.
    oppdater = {pdf.filename(i, "protokoll") for i in pipeline.ufullstendige_protokoller(items)}
    targets += [{**t, "oppdater": True} for t in pdf.targets(items) if t["filename"] in oppdater]
    if oppdater:
        logging.info("%d protokoll(er) mangler møtets vedtak og hentes på nytt", len(oppdater))
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
