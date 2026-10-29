import pytest
import requests

from tingposten import config, nedlaster

PDF = b"%PDF-1.4 test"


def item(nr, vedtak="22", innstilling="11"):
    return {"id": nr, "utvalgId": "571424", "moteDato": "2026-05-20", "referansenr": nr,
            "innstillingFilId": innstilling, "innstillingAlternativBeskrivelse": "Framstilling-x",
            "vedtakFilId": vedtak, "vedtakAlternativBeskrivelse": "Vedtak-x"}


class Response:
    def __init__(self, status=200, content=PDF):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    """Answers from a list (one per request); the last answer repeats."""
    def __init__(self, answers):
        self.headers, self.answers, self.urls = {}, list(answers), []

    def get(self, url, timeout):
        self.urls.append(url)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fetcher(answers):
    clock = Clock()
    return nedlaster.Fetcher(delay_s=5, session=Session(answers), sleep=clock.sleep, clock=clock), clock


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PDF_FETCH", True)


def test_off_by_default_downloads_nothing(tmp_path):
    assert config.ALLOW_PDF_FETCH is False
    client, _ = fetcher([Response()])
    assert nedlaster.last_ned([item("1/26")], tmp_path, client) == {"av": 2}
    assert client.session.urls == []


def test_downloads_missing_pdfs_politely(tmp_path, allowed):
    client, clock = fetcher([Response()])
    have = tmp_path / "pdf/571424/2026-05-20/2-26_protokoll.pdf"
    have.parent.mkdir(parents=True)
    have.write_bytes(PDF)
    counts = nedlaster.last_ned([item("1/26"), item("2/26")], tmp_path, client)
    assert counts == {"lastet_ned": 3, "feil": 0, "gjenstar": 0, "blokkert": False}
    assert (tmp_path / "pdf/571424/2026-05-20/1-26_protokoll.pdf").read_bytes() == PDF
    assert len(client.session.urls) == 3  # the existing file is not fetched again
    assert clock.sleeps == [5, 5]  # a pause before every request but the first
    assert client.session.headers["User-Agent"] == config.USER_AGENT
    assert not list(tmp_path.rglob("*.part"))


def test_items_without_a_vedtak_only_get_the_saksframlegg(tmp_path):
    todo = nedlaster.mangler([item("1/26", vedtak=None)], tmp_path)
    assert [kind for _, kind, _, _ in todo] == ["saksframlegg"]


def test_non_pdf_body_and_missing_file_are_failures_not_stops(tmp_path, allowed):
    client, _ = fetcher([Response(content=b"<html>blocked</html>"), Response(status=404), Response()])
    counts = nedlaster.last_ned([item("1/26", innstilling=None), item("2/26", innstilling=None), item("3/26", innstilling=None)], tmp_path, client)
    assert counts == {"lastet_ned": 1, "feil": 2, "gjenstar": 0, "blokkert": False}
    assert sorted(p.name for p in tmp_path.rglob("*.pdf")) == ["3-26_protokoll.pdf"]


def test_repeated_refusal_stops_the_run(tmp_path, allowed):
    client, clock = fetcher([requests.ConnectionError("tls")])
    counts = nedlaster.last_ned([item("1/26"), item("2/26")], tmp_path, client)
    assert counts["blokkert"] is True and counts["lastet_ned"] == 0 and counts["gjenstar"] == 4
    assert len(client.session.urls) == len(config.BACKOFF_S) + 1  # tried one file, then gave up
    assert list(config.BACKOFF_S) == [s for s in clock.sleeps if s in config.BACKOFF_S]
    assert not list(tmp_path.rglob("*.pdf"))


def test_server_errors_back_off_then_recover(tmp_path, allowed):
    client, clock = fetcher([Response(status=503), Response()])
    counts = nedlaster.last_ned([item("1/26", innstilling=None)], tmp_path, client)
    assert counts["lastet_ned"] == 1
    assert config.BACKOFF_S[0] in clock.sleeps


def test_cap_per_run(tmp_path, allowed, monkeypatch):
    monkeypatch.setattr(config, "MAKS_PDF_PER_KJORING", 3)
    client, _ = fetcher([Response()])
    counts = nedlaster.last_ned([item(f"{n}/26") for n in range(1, 4)], tmp_path, client)
    assert counts == {"lastet_ned": 3, "feil": 0, "gjenstar": 3, "blokkert": False}
