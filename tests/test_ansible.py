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


def test_daily_runs_are_timers_that_never_download_bergens_pdfs_or_publish_stortinget():
    rolle = ANSIBLE / "roles" / "tingposten"
    timere = (rolle / "vars" / "main.yml").read_text()
    # Bergen's daily run without a mode: never --manuell (the PDF download is a person's button press)
    assert "skript: deploy/run-daily.sh\n" in timere and "--manuell" not in timere
    assert "skript: deploy/run-stortinget.sh\n" in timere
    assert "ansible.builtin.cron" in (rolle / "tasks" / "daglig.yml").read_text()  # only to remove the old entries
    assert "state: absent" in (rolle / "tasks" / "daglig.yml").read_text()


def test_the_morning_run_publishes_by_a_switch():
    rolle = ANSIBLE / "roles" / "tingposten"
    assert "tingposten_daglig_publiser: false" in (rolle / "defaults" / "main.yml").read_text()
    assert "tingposten_daglig_publiser: true" in (ANSIBLE / "host_vars" / "pi.yml").read_text()
    assert '/PUBLISER"' in (rolle / "tasks" / "daglig.yml").read_text()
