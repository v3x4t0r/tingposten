"""Shared test setup: build tests run against neutral site settings, not the live config,
so setting the real contact address or editor doesn't change what other tests expect.
Tests about those settings set them explicitly with monkeypatch."""
import pytest

from tingposten import config


@pytest.fixture(autouse=True)
def noytrale_innstillinger(monkeypatch):
    monkeypatch.setattr(config, "KONTAKT_EPOST", None)
