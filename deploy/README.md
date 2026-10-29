# Drift på Raspberry Pi

Tingposten kjører én gang i døgnet på Pi-en (Raspberry Pi OS Trixie, 64-bit). Den henter nye saker fra Bergen kommune, bygger siden og pusher den til `gh-pages`. Fra fase 1 kommer også skribent, faktasjekker og redaktør. Alt kjører i et eget Python-miljø (uv) under vanlig bruker, adskilt fra Home Assistant, Jellyfin og Navidrome.

Docker er allerede installert på Pi-en, så en container er et alternativ hvis miljøet en gang må isoleres mer. Foreløpig holder et eget Python-miljø: jobben er liten, og cron er enklere å feilsøke.

## Første gangs oppsett

```sh
git clone https://github.com/midttun/tingposten.git ~/tingposten
~/tingposten/deploy/setup-pi.sh
```

Skriptet installerer uv, lager Python-miljøet, lager `.env` (bare lesbar for deg) og en egen SSH-nøkkel for push. Til slutt skriver det ut de neste stegene:

1. Fyll inn API-nøkler og kontaktadresse i `.env`.
2. Legg inn nøkkelen som *deploy key* med skrivetilgang på GitHub. Nøkkelen gir bare tilgang til dette ene repoet.
3. Legg inn `Host github-tingposten` i `~/.ssh/config` (skriptet viser teksten).
4. Sjekk at tidssonen er Europe/Oslo, og legg inn cron-jobben (se `crontab.example`).

Skriptet kan kjøres på nytt uten å ødelegge noe.

## Kjøre én gang for hånd

```sh
~/tingposten/deploy/run-daily.sh; echo "exit $?"
```

Bare henting, uten publisering:

```sh
cd ~/tingposten && uv run --no-sync tingposten hent
```

## Logger

Hver kjøring skriver til `data/logs/ÅÅÅÅ-MM-DD.log`. Logger eldre enn 60 dager slettes automatisk.

Exit-kode 2 betyr at Bergen kommunes server nekter oss. Da publiseres ingenting den dagen. Neste kjøring prøver igjen; det er ingen grunn til å kjøre på nytt med en gang.

## Stoppe publisering (kill switch)

Henting fortsetter, men ingenting publiseres så lenge en fil som heter `STOP` finnes:

- **På Pi-en:** `touch ~/tingposten/STOP`. Slett filen for å starte igjen.
- **Fra GitHub (også fra mobilen):** legg en fil som heter `STOP` i rotmappen på `main`. Slett den for å starte igjen.

## Oppdatere koden

Pi-en henter aldri ny kode av seg selv. Repoet er offentlig, og en push til GitHub skal ikke endre det som kjører hjemme uten at du har sett på det. Oppdater for hånd:

```sh
cd ~/tingposten
git pull --ff-only
uv sync --frozen --no-dev
```

## Hemmeligheter

API-nøklene ligger i `.env` på Pi-en, aldri i repoet. `data/` og `site/` er heller ikke i git; der ligger rådata som kan inneholde navn på privatpersoner.
