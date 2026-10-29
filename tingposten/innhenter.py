"""Innhenter: fetch agenda items, store them locally and log what changed.

Store: data/bergen/agendapunkter/<utvalgId>.json, raw API items keyed by id.
Log:   data/bergen/endringer.jsonl, one line per change:
         ny         – item seen for the first time
         vedtatt    – vedtakFilId went from null to an id ("something was decided")
         endret     – any other field changed (lists the field names)
         forsvunnet – item no longer returned by the API (kept in the store)
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .bk360 import Client

log = logging.getLogger(__name__)


def years_to_fetch(today: date) -> list[int]:
    # December protocols are published in January, so keep last year open a while.
    return sorted({today.year, (today - timedelta(days=60)).year})


def diff(old: dict[str, dict], new: list[dict], complete: bool) -> list[dict]:
    """Changes between the stored items for one organ/year and a fresh fetch."""
    changes = []
    seen = set()
    for item in new:
        seen.add(item["id"])
        before = old.get(item["id"])
        if before is None:
            changes.append(_change("ny", item))
            if item.get("vedtakFilId"):
                changes.append(_change("vedtatt", item))
            continue
        fields = sorted(k for k in item.keys() | before.keys() if item.get(k) != before.get(k))
        if not fields:
            continue
        if before.get("vedtakFilId") is None and item.get("vedtakFilId"):
            changes.append(_change("vedtatt", item))
            fields.remove("vedtakFilId")
        if fields:
            changes.append(_change("endret", item, felt=fields))
    if complete:
        changes += [_change("forsvunnet", item) for i, item in old.items() if i not in seen]
    return changes


def _change(kind: str, item: dict, **extra) -> dict:
    return {
        "type": kind,
        "id": item["id"],
        "utvalgId": item["utvalgId"],
        "moteDato": item["moteDato"],
        "tittel": item["tittel"],
        **extra,
    }


def run(years: list[int], utvalg_ids: list[str], data_dir: Path = config.DATA_DIR) -> int:
    client = Client()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    organs = client.utvalg()
    _write_json(data_dir / "utvalg.json", organs)
    known = {o["id"] for o in organs}
    for uid in utvalg_ids:
        if uid not in known:
            # Organ IDs are expected to change after the 2027 election.
            log.error("utvalgId %s (%s) is no longer in the organ list", uid, config.TIER1.get(uid))

    total_changes = 0
    for uid in utvalg_ids:
        path = data_dir / "agendapunkter" / f"{uid}.json"
        store = _read_json(path, {})
        for year in years:
            fetched, complete = client.agendapunkter(uid, year)
            old = {k: v for k, v in store.items() if v.get("arstall") == year}
            changes = diff(old, fetched, complete)
            store.update({i["id"]: i for i in fetched})
            _write_json(path, store)
            _append_log(data_dir / "endringer.jsonl", now, changes)
            total_changes += len(changes)
            counts = {k: sum(c["type"] == k for c in changes) for k in ("ny", "vedtatt", "endret", "forsvunnet")}
            log.info("%s %s: %d items, %s", config.TIER1.get(uid, uid), year, len(fetched), counts)
    return total_changes


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _append_log(path: Path, now: str, changes: list[dict]) -> None:
    if not changes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for c in changes:
            f.write(json.dumps({"tid": now, **c}, ensure_ascii=False) + "\n")
