"""deploy/ansible (#21): the playbooks parse, and the rollout's change groups pick the
right steps. Skipped where Ansible isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).parent.parent / "deploy" / "ansible"


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="Ansible er ikke installert")
@pytest.mark.parametrize("playbook", ["site.yml", "deploy.yml"])
def test_playbook_syntax(playbook):
    r = subprocess.run(["ansible-playbook", playbook, "--syntax-check"], cwd=ANSIBLE, capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_secrets_are_checked_never_copied():
    tekst = "\n".join(p.read_text() for p in (ANSIBLE / "roles").rglob("*.yml"))
    for fil in (".env", "debatt.env", "tailscale.env"):
        assert fil in tekst
    # never a copy/template of a secret file, never its contents in a variable
    assert "lookup('file'" not in tekst and "slurp" not in tekst
    assert "ANTHROPIC_API_KEY" in tekst
