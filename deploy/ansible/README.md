# Tingposten på Pi-en med Ansible (#21)

Grunnoppsettet av Pi-en (Docker, Hailo, kamera-stakken) ligger i `~/Projects/rpi-setup`. Denne mappen gjør bare Tingposten. Kjør alt fra `deploy/ansible/` på arbeidsmaskinen. Pi-en nås over Tailscale (`inventory.yml`).

## Rulle ut det som ligger på main

```sh
ansible-playbook deploy.yml                    # hent, bygg om det som er endret, bygg og publiser siden
ansible-playbook deploy.yml -e publiser=false  # det samme, uten å bygge og publisere siden
ansible-playbook deploy.yml --check            # hva som ville skjedd, uten å endre noe
```

Steg for steg:

1. **Koden** spoles fram til `origin/main`. Den stopper hvis Pi-ens kopi har lokale endringer.
2. **Python-miljøet** synkes når `pyproject.toml` eller `uv.lock` er endret.
3. **Kontrollpanelet** startes på nytt når koden er endret.
4. **Kommentarserveren** bygges på nytt bare når noe i imaget er endret (`tingposten/`, `pyproject.toml`, `uv.lock`, `README.md`, `Dockerfile`, `compose.yml`). Etterpå sjekkes det at den svarer.
5. **Brannmuren:** endringer i den trenger root, så playbooken sier fra (se under).
6. **Siden** bygges og publiseres med `deploy/run-daily.sh --publiser`. STOP gjelder også her, og kjøringen vises i kontrollpanelet.

En oppsummering til slutt viser hva som ble gjort.

## Sette opp, eller få Pi-en tilbake til det repoet sier

```sh
ansible-playbook site.yml --check --diff        # hva en kjøring ville endret
ansible-playbook site.yml -K                    # alt; -K spør om sudo-passordet (brannmuren, linger)
ansible-playbook site.yml --skip-tags system    # alt uten sudo
```

Playbooken dekker:
- repoet og push-adressen
- uv
- Python-miljøet (`uv sync --frozen`, uten ekstra pakker, som på Pi-en i dag)
- kontrollpanelet som brukertjeneste
- kommentarserveren (Docker Compose)
- brannmuren (`/usr/local/sbin`, systemd)
- den daglige kjøringen i cron, som er av (`tingposten_daglig_kjoring` i `host_vars/pi.yml`)

Den kan trygt kjøres flere ganger. På en Pi som er som beskrevet, endrer den ingenting.

## Hemmeligheter

`.env`, `deploy/debatt/debatt.env` og `deploy/debatt/tailscale.env` blir liggende på Pi-en og skrives for hånd (se `deploy/README.md` og `deploy/debatt/README.md`). Playbooken:

- sjekker at filene finnes, og stopper med beskjed hvis en mangler;
- setter mode 600;
- sjekker at `ANTHROPIC_API_KEY` er tom, så modellene går på abonnementet.

Den leser aldri innholdet og skriver det aldri ut.

## Innstillinger

Standardverdiene ligger i `roles/tingposten/defaults/main.yml`. Det som gjelder akkurat denne Pi-en, ligger i `host_vars/pi.yml`.
