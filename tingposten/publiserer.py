"""Publiserer: put the built site online.

Targets (config.PUBLISER_TIL):
- "statichost-git" / "github-pages": mirror site/ into the gh-pages branch and
  push it; statichost.eu (webhook) or GitHub Pages serves that branch. For
  statichost the branch also gets statichost.yml ("public: ." = no build).
  The branch lives in its own worktree under data/ (ignored by git), so the
  main working tree and the main branch are never touched or pushed.
- "statichost-upload": zip site/ and upload it to statichost.eu (direct
  upload, the same request as their shcli script). The key comes from .env.

A file named STOP in the repo root is the kill switch for both: nothing is
uploaded, committed or pushed.
"""

import io
import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

PLACEHOLDER = """<!doctype html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tingposten</title>
<style>
:root { --bg: #fbfaf7; --fg: #1d1d1b; --muted: #5f5e5a; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ecebe6; --muted: #a3a29c; }
}
body {
  margin: 0; padding: 0 16px; background: var(--bg); color: var(--fg);
  font: 18px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 34rem; margin: 18vh auto; }
h1 { margin: 0 0 .25em; font-size: 2.2rem; letter-spacing: -.01em; }
p { margin: 0 0 1em; }
.muted { color: var(--muted); }
</style>
</head>
<body>
<main>
<h1>Tingposten</h1>
<p>Hva Bergen kommune faktisk vedtar, uten politisk prat: hver sak i samme lesbare format, de viktigste først, alt dokumentert med kilde.</p>
<p class="muted">Kommer snart.</p>
</main>
</body>
</html>
"""


def build_placeholder(site_dir: Path = config.SITE_DIR) -> None:
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text(PLACEHOLDER)
    (site_dir / ".nojekyll").write_text("")
    if config.CNAME:
        (site_dir / "CNAME").write_text(config.CNAME + "\n")


def publiser(message: str = "Publiser") -> bool:
    """Publish site/ to the configured target. False if the kill switch stopped it."""
    if config.PUBLISER_TIL == "statichost-upload":
        return publish_statichost()
    return publish(message=message, statichost=config.PUBLISER_TIL == "statichost-git")


def publish_statichost(
    site_dir: Path = config.SITE_DIR,
    repo_dir: Path = config.ROOT,
    site: str | None = None,
    apikey: str | None = None,
    session=None,
) -> bool:
    """Upload site_dir to statichost.eu. Raises RuntimeError if the upload fails."""
    if (repo_dir / "STOP").exists():
        log.warning("STOP file present in %s, skipping publish", repo_dir)
        return False
    site = site or config.STATICHOST_SITE
    apikey = apikey or os.environ.get("STATICHOST_APIKEY", "")
    if not apikey:
        raise RuntimeError("STATICHOST_APIKEY mangler i .env (lag en API-nøkkel på builder.statichost.eu/account)")
    if not (site_dir / "index.html").exists():
        raise RuntimeError(f"{site_dir} har ingen index.html; kjør `tingposten bygg` først")
    body = zip_site(site_dir)
    response = (session or requests).post(
        f"{config.STATICHOST_BUILDER}/{site}/drop",
        data=body,
        headers={"Authorization": f"Bearer {apikey}", "Content-Type": "application/zip",
                 "Accept": "text/plain", "User-Agent": config.USER_AGENT},
        timeout=300,
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"statichost svarte {response.status_code}: {response.text.strip()[:300]}")
    log.info("uploaded %s (%d KB) to %s.statichost.eu: %s", site_dir, len(body) // 1024, site, response.text.strip()[:200])
    return True


def zip_site(site_dir: Path) -> bytes:
    """The contents of site_dir (not the folder itself) as a zip, in a fixed order."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(p for p in site_dir.rglob("*") if p.is_file()):
            z.write(path, path.relative_to(site_dir).as_posix())
    return buf.getvalue()


def publish(
    site_dir: Path = config.SITE_DIR,
    repo_dir: Path = config.ROOT,
    branch: str = config.PAGES_BRANCH,
    message: str = "Publiser",
    statichost: bool = False,
) -> bool:
    """Commit site_dir to `branch` and push it. False if the kill switch stopped it."""
    if (repo_dir / "STOP").exists():
        log.warning("STOP file present in %s, skipping publish", repo_dir)
        return False

    has_origin = _git(repo_dir, "remote", "get-url", "origin", check=False).returncode == 0
    if has_origin:
        # Fails harmlessly when the branch doesn't exist on origin yet.
        _git(repo_dir, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)

    worktree = repo_dir / "data" / ".gh-pages"
    _ensure_worktree(repo_dir, worktree, branch)
    _mirror(site_dir, worktree)
    if statichost:
        # statichost.eu reads this from the branch root: publish the files as-is, no build.
        (worktree / "statichost.yml").write_text("public: .\n")

    _git(worktree, "add", "-A")
    if _git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("site unchanged, nothing to commit")
    else:
        _git(worktree, "commit", "-q", "-m", message)
        log.info("committed %s to %s", message, branch)

    if has_origin:
        _git(worktree, "push", "-q", "origin", f"{branch}:{branch}")
        log.info("pushed %s to origin", branch)
    else:
        log.warning("no origin remote, committed locally but did not push")
    return True


def _ensure_worktree(repo_dir: Path, worktree: Path, branch: str) -> None:
    if (worktree / ".git").exists():
        return
    if worktree.exists():
        shutil.rmtree(worktree)
    _git(repo_dir, "worktree", "prune")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _ref_exists(repo_dir, f"refs/heads/{branch}"):
        _git(repo_dir, "worktree", "add", "-q", str(worktree), branch)
    elif _ref_exists(repo_dir, f"refs/remotes/origin/{branch}"):
        _git(repo_dir, "worktree", "add", "-q", "--track", "-b", branch, str(worktree), f"origin/{branch}")
    else:
        # `worktree add --orphan` needs git 2.42; Raspberry Pi OS ships older.
        _git(repo_dir, "worktree", "add", "-q", "--detach", "--no-checkout", str(worktree))
        _git(worktree, "checkout", "-q", "--orphan", branch)


def _mirror(src: Path, dst: Path) -> None:
    for child in dst.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _ref_exists(repo_dir: Path, ref: str) -> bool:
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result
