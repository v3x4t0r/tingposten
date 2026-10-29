import copy
from datetime import date

from tingposten.innhenter import diff, years_to_fetch

ITEM = {
    "id": "271025:9089478:agendapunkt:bk360",
    "utvalgId": "571424",
    "moteDato": "2026-09-23",
    "tittel": "Reguleringsplan for Eksempelveien",
    "arstall": 2026,
    "innstillingFilId": "16665727",
    "vedtakFilId": None,
}


def kinds(changes):
    return [c["type"] for c in changes]


def test_new_item():
    assert kinds(diff({}, [ITEM], complete=True)) == ["ny"]


def test_new_item_already_decided():
    item = {**ITEM, "vedtakFilId": "999"}
    assert kinds(diff({}, [item], complete=True)) == ["ny", "vedtatt"]


def test_unchanged():
    assert diff({ITEM["id"]: ITEM}, [copy.deepcopy(ITEM)], complete=True) == []


def test_vedtak_published():
    item = {**ITEM, "vedtakFilId": "999"}
    assert kinds(diff({ITEM["id"]: ITEM}, [item], complete=True)) == ["vedtatt"]


def test_vedtak_published_and_title_changed():
    item = {**ITEM, "vedtakFilId": "999", "tittel": "Ny tittel"}
    changes = diff({ITEM["id"]: ITEM}, [item], complete=True)
    assert kinds(changes) == ["vedtatt", "endret"]
    assert changes[1]["felt"] == ["tittel"]


def test_vedtak_file_replaced_is_endret_not_vedtatt():
    old = {**ITEM, "vedtakFilId": "999"}
    new = {**ITEM, "vedtakFilId": "1000"}
    changes = diff({ITEM["id"]: old}, [new], complete=True)
    assert kinds(changes) == ["endret"]
    assert changes[0]["felt"] == ["vedtakFilId"]


def test_disappeared_only_when_fetch_complete():
    old = {ITEM["id"]: ITEM}
    assert kinds(diff(old, [], complete=True)) == ["forsvunnet"]
    assert diff(old, [], complete=False) == []


def test_years_to_fetch():
    assert years_to_fetch(date(2026, 10, 29)) == [2026]
    assert years_to_fetch(date(2027, 1, 15)) == [2026, 2027]
    assert years_to_fetch(date(2027, 3, 15)) == [2027]
