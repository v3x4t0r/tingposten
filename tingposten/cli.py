import argparse
import logging
import sys
from datetime import date, datetime

from . import config, innhenter, publiserer
from .bk360 import Blocked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tingposten")
    sub = parser.add_subparsers(dest="cmd", required=True)

    hent = sub.add_parser("hent", help="fetch agenda items and log changes")
    hent.add_argument("--year", type=int, action="append", help="default: this year (+ last year in Jan/Feb)")
    hent.add_argument("--utvalg", action="append", help="utvalgId; default: Tier 1 organs")

    publiser = sub.add_parser("publiser", help="commit site/ to gh-pages and push")
    publiser.add_argument("--placeholder", action="store_true", help="build the 'kommer snart' page first")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
