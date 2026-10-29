import subprocess

import pytest

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
