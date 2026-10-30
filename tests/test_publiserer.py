import subprocess

import pytest

from tingposten import config, publiserer
from tingposten.publiserer import build_placeholder, publish

BRANCH = "gh-pages"


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("main branch\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def origin(tmp_path, repo):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    return origin


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    build_placeholder(site)
    return site


def files_on(repo, ref):
    return sorted(git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def commit_count(repo, ref):
    return int(git(repo, "rev-list", "--count", ref))


def test_first_publish_creates_branch_on_origin(repo, origin, site):
    assert publish(site, repo, BRANCH, "first") is True
    assert files_on(origin, BRANCH) == [".nojekyll", "index.html"]
    # gh-pages is an orphan: no main history in it
    assert commit_count(origin, BRANCH) == 1


def test_cname_written_when_domain_configured(tmp_path, repo, origin, monkeypatch):
    import tingposten.config as config
    monkeypatch.setattr(config, "CNAME", "tingposten.no")
    site = tmp_path / "site"
    build_placeholder(site)
    publish(site, repo, BRANCH, "first")
    assert files_on(origin, BRANCH) == [".nojekyll", "CNAME", "index.html"]
    assert git(origin, "show", f"{BRANCH}:CNAME") == "tingposten.no"


def test_main_branch_untouched(repo, origin, site):
    head = git(repo, "rev-parse", "main")
    publish(site, repo, BRANCH, "first")
    assert git(repo, "rev-parse", "main") == head
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert (repo / "README.md").read_text() == "main branch\n"


def test_unchanged_site_makes_no_commit(repo, origin, site):
    publish(site, repo, BRANCH, "first")
    publish(site, repo, BRANCH, "second")
    assert commit_count(origin, BRANCH) == 1


def test_changed_and_removed_files(repo, origin, site):
    (site / "old.html").write_text("old")
    publish(site, repo, BRANCH, "first")
    (site / "old.html").unlink()
    (site / "bergen").mkdir()
    (site / "bergen" / "index.html").write_text("ny")
    publish(site, repo, BRANCH, "second")
    assert files_on(origin, BRANCH) == [".nojekyll", "bergen/index.html", "index.html"]
    assert commit_count(origin, BRANCH) == 2


def test_kill_switch(repo, origin, site):
    (repo / "STOP").write_text("")
    assert publish(site, repo, BRANCH, "first") is False
    assert git(origin, "branch", "--list") == ""
    assert not (repo / "data").exists()


def test_no_origin_commits_locally(repo, site):
    assert publish(site, repo, BRANCH, "first") is True
    assert files_on(repo, BRANCH) == [".nojekyll", "index.html"]


def test_fresh_clone_tracks_existing_remote_branch(tmp_path, repo, origin, site):
    publish(site, repo, BRANCH, "first")
    git(repo, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (site / "extra.html").write_text("x")
    publish(site, clone, BRANCH, "from clone")
    assert commit_count(origin, BRANCH) == 2
    assert "extra.html" in files_on(origin, BRANCH)



class FakeSession:
    def __init__(self, status=200, text="Deployed"):
        self.calls, self.status, self.text = [], status, text

    def post(self, url, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return type("R", (), {"status_code": self.status, "text": self.text})()


def test_statichost_uploads_the_contents_as_a_zip(tmp_path):
    import io
    import zipfile
    site = tmp_path / "site"
    (site / "bergen").mkdir(parents=True)
    (site / "index.html").write_text("<h1>hei</h1>")
    (site / "bergen" / "index.html").write_text("forside")
    s = FakeSession()
    assert publiserer.publish_statichost(site, tmp_path, site="tingposten", apikey="hemmelig", session=s)
    call = s.calls[0]
    assert call["url"] == "https://builder.statichost.eu/tingposten/drop"
    assert call["headers"]["Authorization"] == "Bearer hemmelig"
    assert call["headers"]["Content-Type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(call["data"])).namelist()
    assert names == ["bergen/index.html", "index.html"]  # contents at the root, not site/…


def test_statichost_stop_missing_key_and_errors(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("x")
    monkeypatch.delenv("STATICHOST_APIKEY", raising=False)
    with pytest.raises(RuntimeError, match="STATICHOST_APIKEY"):
        publiserer.publish_statichost(site, tmp_path, session=FakeSession())
    with pytest.raises(RuntimeError, match="401"):
        publiserer.publish_statichost(site, tmp_path, apikey="k", session=FakeSession(401, "Unauthorized"))
    (tmp_path / "STOP").write_text("")
    s = FakeSession()
    assert publiserer.publish_statichost(site, tmp_path, apikey="k", session=s) is False and s.calls == []



def test_statichost_git_branch_gets_no_build_config(repo, origin, site):
    publish(site, repo, BRANCH, message="Publiser", statichost=True)
    files = subprocess.run(["git", "-C", str(origin), "ls-tree", "--name-only", BRANCH],
                           capture_output=True, text=True, check=True).stdout.split()
    assert "statichost.yml" in files and "index.html" in files
    shown = subprocess.run(["git", "-C", str(origin), "show", f"{BRANCH}:statichost.yml"],
                           capture_output=True, text=True, check=True).stdout
    assert shown == "public: .\n"


def test_trigger_build_posts_to_the_site_with_the_key():
    s = FakeSession(200, "Build queued")
    publiserer.trigger_build(site="tingposten", apikey="hemmelig", session=s)
    assert s.calls[0]["url"] == "https://builder.statichost.eu/tingposten"
    assert s.calls[0]["headers"]["Authorization"] == "Bearer hemmelig"
    s = FakeSession()
    publiserer.trigger_build(site="tingposten", apikey="", session=s)
    assert "Authorization" not in s.calls[0]["headers"]
    with pytest.raises(RuntimeError, match="401"):
        publiserer.trigger_build(site="tingposten", apikey="feil", session=FakeSession(401, "Unauthorized"))


def test_publiser_git_triggers_a_build_but_not_when_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "PUBLISER_TIL", "statichost-git")
    monkeypatch.setattr(publiserer, "trigger_build", lambda: calls.append("bygg"))
    monkeypatch.setattr(publiserer, "publish", lambda message, statichost: statichost)
    assert publiserer.publiser() is True and calls == ["bygg"]
    monkeypatch.setattr(publiserer, "publish", lambda message, statichost: False)
    assert publiserer.publiser() is False and calls == ["bygg"]
