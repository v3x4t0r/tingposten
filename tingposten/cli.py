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

    publiser = sub.add_parser("publiser", help="commit site/ to gh-pages and push")
    publiser.add_argument("--placeholder", action="store_true", help="build the 'kommer snart' page first")

    urler = sub.add_parser("pdf-urler", help="build PDF download URLs for a meeting (fetches nothing)")
    urler.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    urler.add_argument("--dato", help="meeting date YYYY-MM-DD; default: the newest stored meeting")
    urler.add_argument("--all", action="store_true", help="include noise (innkalling, spørsmål, …)")
    urler.add_argument("--script", type=Path, help="also write a polite wget script to this path")

    arkp = sub.add_parser("ark", help="worksheet (CSV) for sorting a downloaded meeting by hand")
    arkp.add_argument("--utvalg", required=True, help="utvalgId, e.g. 571424")
    arkp.add_argument("--dato", required=True, help="meeting date YYYY-MM-DD")
    arkp.add_argument("--out", type=Path, help="default: data/bergen/ark/<utvalg>-<dato>.csv")

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
            publiserer.publish(message=f"Publiser {datetime.now():%Y-%m-%d %H:%M}")
        except RuntimeError as e:
            logging.error("%s", e)
            return 1
    elif args.cmd == "pdf-urler":
        return _pdf_urler(args)
    elif args.cmd == "ark":
        return _ark(args)
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


if __name__ == "__main__":
    sys.exit(main())
