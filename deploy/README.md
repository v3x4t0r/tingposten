# Drift på Raspberry Pi

Tingposten kjører én gang i døgnet på Pi-en (Raspberry Pi OS Trixie, 64-bit, Python 3.13, git 2.47). Alt kjører i et eget Python-miljø (uv) under vanlig bruker, adskilt fra Home Assistant, Jellyfin og Navidrome.

Docker er allerede installert på Pi-en, så en container er et alternativ hvis miljøet en gang må isoleres mer. Foreløpig holder et eget Python-miljø: jobben er liten, og cron er enklere å feilsøke.

## Hva den daglige kjøringen gjør

`deploy/run-daily.sh` kjører stegene i rekkefølge og skriver alt til loggen:

1. **hent** – nye og endrede agendapunkter fra Bergen kommune. Nekter serveren oss, stopper kjøringen for i dag (exit 2).
2. **last-ned** – manglende saksframlegg og protokoller. **Gjør ingenting** før `ALLOW_PDF_FETCH` er slått på i `tingposten/config.py`, fordi robots.txt sier nei til automatisk henting av filene. Slå det på først når Bystyrets kontor har sagt ja. Da hentes høyst `MAKS_PDF_PER_KJORING` filer per kjøring, én om gangen med pause imellom.
   Inntil da skriver steget i stedet `data/bergen/last-ned-mangler.sh`: ett skript med alle PDF-ene som mangler. Det henter ingenting selv; det er du som kjører det (se «PDF-er for hånd» under).
3. **kjor** – skribent (Claude), faktasjekker (Codex), klassifiserer og redaktør for hver sak som har PDF-er. Går en modell tom for kvote, fortsetter kjøringen med resten, og neste kjøring tar det som mangler. Feil her er derfor bare en advarsel.
4. **bygg** – lager siden i `site/` av det som er publiserbart. Feiler byggingen, publiseres ingenting.
5. **publiser** – laster `site/` opp til statichost.eu (eller pusher til `gh-pages`, se `PUBLISER_TIL` i `tingposten/config.py`), men **bare hvis filen `PUBLISER` finnes** i repo-roten. Uten den blir alt liggende lokalt.

## Publisering på statichost.eu

Siden bygges lokalt (eller på Pi-en), og `publiser` committer den ferdige siden til grenen `gh-pages` og pusher den. statichost.eu publiserer grenen når den får beskjed fra GitHub (webhook). Koden på `main` pushes ikke.

Gratisplanen (Hobby) gir ett nettsted, 10 GB trafikk og eget domene. Siden er liten (rundt 7 KB per side), så det holder lenge. Serverne står i Stockholm og eies av et europeisk selskap.

Oppsett, én gang:

1. **statichost:** lag nettstedet, koble det til repoet `v3x4t0r/tingposten` og velg grenen **`gh-pages`** (ikke `main`). Byggeoppsett trengs ikke: grenen har en `statichost.yml` med `public: .`, som betyr «publiser filene som de er».
2. **GitHub:** Settings → Webhooks → Add webhook. Payload URL: `https://builder.statichost.eu/<navnet på nettstedet>`, «Just the push event».
3. **GitHub Pages av:** Settings → Pages → slå av, ellers ligger siden ute to steder.
4. Test: `uv run tingposten bygg && uv run tingposten publiser`. Siden ligger da på `https://<navnet>.statichost.eu/`.
5. Eget domene (tingposten.no) legges inn hos statichost og hos domeneregistraren; se statichost.eu/docs/domains/.

Vil du heller laste opp direkte uten git, sett `PUBLISER_TIL = "statichost-upload"` i `tingposten/config.py` og legg en API-nøkkel i `.env` (`STATICHOST_APIKEY`).

## PDF-er for hånd

Så lenge automatisk henting er av, laster du ned PDF-ene selv. Det er lite: rundt 100 filer i måneden (ett bystyremøte og 4–5 byrådsmøter), altså et par minutter i uka.

```sh
bash ~/tingposten/data/bergen/last-ned-mangler.sh
```

Skriptet lages på nytt hver natt. Det henter én fil om gangen med 5 s pause og identifiserbar User-Agent. Filer som alt finnes hoppes over, så et avbrutt løp kan bare startes på nytt. Svarer serveren med feil, stopper skriptet med en gang. Kjør det fra hjemmenettet, ikke over VPN. Neste nattlige kjøring skriver kortene. Skriptet finnes bare når noe mangler.

For å lage det med en gang: `uv run tingposten pdf-urler --mangler --script data/bergen/last-ned-mangler.sh`.

## Første gangs oppsett

```sh
git clone https://github.com/v3x4t0r/tingposten.git ~/tingposten
~/tingposten/deploy/setup-pi.sh
```

Skriptet installerer uv og Claude Code, lager Python-miljøet, lager `.env` (bare lesbar for deg) og en egen SSH-nøkkel for push. Til slutt skriver det ut de neste stegene:

1. Fyll inn kontaktadressen (`TINGPOSTEN_CONTACT`) i `.env`. La `ANTHROPIC_API_KEY` og `OPENAI_API_KEY` stå tomme: modellene kjører på abonnementene dine gjennom kommandolinjeverktøyene, ikke på API-nøkler. En utfylt `ANTHROPIC_API_KEY` får `claude` til å fakturere API-et i stedet for abonnementet.
2. Logg inn de to modellverktøyene (se under).
3. Legg inn nøkkelen som *deploy key* med skrivetilgang på GitHub. Nøkkelen gir bare tilgang til dette ene repoet.
4. Legg inn `Host github-tingposten` i `~/.ssh/config` (skriptet viser teksten).
5. Sjekk at tidssonen er Europe/Oslo, og legg inn cron-jobben (se `crontab.example`).

Skriptet kan kjøres på nytt uten å ødelegge noe.

## Modellverktøyene: Claude Code og Codex

Begge bruker abonnementene dine, ikke API-nøkler, og deler kvote med din egen bruk. Treffer en kjøring grensen, blir noen saker stående til neste dag. Kjøringen klokken 06.00 er lagt slik at kvoten som regel er fornyet.

**Claude Code** (skribent og klassifiserer):

- Installer: `curl -fsSL https://claude.ai/install.sh | bash` (Anthropics eget installasjonsprogram, fungerer på arm64; legger `claude` i `~/.local/bin`). Alternativ med Node 18+: `npm install -g @anthropic-ai/claude-code`.
- Logg inn uten skjerm: `claude setup-token`. Den viser en lenke du åpner på mobil eller PC, og skriver ut en langvarig nøkkel. Legg den i `.env` som `CLAUDE_CODE_OAUTH_TOKEN=…`. Tingposten sender `.env` videre til `claude`.

**Codex** (faktasjekker):

- Installer, enten med Node 18+: `sudo apt install nodejs npm && sudo npm install -g @openai/codex@0.164.0`, eller last ned `codex-aarch64-unknown-linux-musl.tar.gz` fra Codex sine utgivelser på GitHub og legg `codex` i `~/.local/bin`. 0.164.0 er versjonen pipelinen er testet med.
- Logg inn uten skjerm: `codex login --device-auth`. Åpne lenken den viser, og skriv inn koden. Sjekk med `codex login status`. Codex fornyer innloggingen selv.

Test begge:

```sh
cd ~/tingposten && set -a && . ./.env && set +a && claude -p "Svar bare: ok"
codex exec --skip-git-repo-check -s read-only "Svar bare: ok"
```

## Slå publisering av og på

Publisering er **av** til du slår den på:

```sh
touch ~/tingposten/PUBLISER   # på
rm ~/tingposten/PUBLISER      # av
```

`STOP` vinner alltid over `PUBLISER`:

- **På Pi-en:** `touch ~/tingposten/STOP`. Slett filen for å starte igjen.
- **Fra GitHub (også fra mobilen):** legg en fil som heter `STOP` i rotmappen på `main`. Slett den for å starte igjen.

Henting, skriving og bygging fortsetter uansett; bare publiseringen hoppes over.

## Kjøre én gang for hånd

```sh
~/tingposten/deploy/run-daily.sh; echo "exit $?"
```

Bare ett steg:

```sh
cd ~/tingposten
uv run --no-sync tingposten hent
uv run --no-sync tingposten kjor
uv run --no-sync tingposten bygg
```

## Logger

Hver kjøring skriver til `data/logs/ÅÅÅÅ-MM-DD.log`. Logger eldre enn 60 dager slettes automatisk.

Exit-kode 2 betyr at Bergen kommunes server nekter oss. Da skjer ingenting mer den dagen. Neste kjøring prøver igjen; det er ingen grunn til å kjøre på nytt med en gang.

## Oppdatere koden

Pi-en henter aldri ny kode av seg selv. En push til GitHub skal ikke endre det som kjører hjemme uten at du har sett på det. Oppdater for hånd:

```sh
cd ~/tingposten
git pull --ff-only
uv sync --frozen --no-dev
```

## Hemmeligheter

Innloggingen for Claude ligger i `.env` og Codex sin i `~/.codex/` på Pi-en, aldri i repoet. `data/` og `site/` er heller ikke i git; der ligger rådata som kan inneholde navn på privatpersoner.
