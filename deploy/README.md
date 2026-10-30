# Drift på Raspberry Pi

Tingposten kjører én gang i døgnet på Pi-en (Raspberry Pi OS Trixie, 64-bit, Python 3.13, git 2.47). Alt kjører i et eget Python-miljø (uv) under vanlig bruker, adskilt fra Home Assistant, Jellyfin og Navidrome.

Docker er allerede installert på Pi-en, så en container er et alternativ hvis miljøet en gang må isoleres mer. Foreløpig holder et eget Python-miljø: jobben er liten, og cron er enklere å feilsøke.

## Hva den daglige kjøringen gjør

`deploy/run-daily.sh` kjører stegene i rekkefølge og skriver alt til loggen:

1. **hent** – nye og endrede agendapunkter fra Bergen kommune. Nekter serveren oss, stopper kjøringen for i dag (exit 2).
2. **last-ned** – manglende saksframlegg og protokoller. **Gjør ingenting** før `ALLOW_PDF_FETCH` er slått på i `tingposten/config.py`, fordi robots.txt sier nei til automatisk henting av filene. Slå det på først når Bystyrets kontor har sagt ja. Da hentes høyst `MAKS_PDF_PER_KJORING` filer per kjøring, én om gangen med pause imellom.
   Inntil da skriver steget i stedet `data/bergen/last-ned-mangler.sh`: ett skript med alle PDF-ene som mangler. Det henter ingenting selv; det er du som kjører det (se «PDF-er for hånd» under).
3. **kjor** – skribent (Claude), faktasjekker (Codex), klassifiserer og redaktør for hver sak som har PDF-er. Går en modell tom for kvote, fortsetter kjøringen med resten, og neste kjøring tar det som mangler. Feil her er derfor bare en advarsel.
   Et nytt utkast som ennå ikke er faktasjekket (for eksempel fordi Codex gikk tom for kvote, eller fordi skribentreglene fikk nytt versjonsnummer og alle kort i vinduet skrives på nytt) erstatter **aldri** et publisert kort eller en publisert brødtekst: redaktøren beholder den sjekkede teksten og logger «nytt utkast uten fersk faktasjekk; det publiserte beholdes». Tallet `venter` i kjor-loggen sier hvor mange som står slik. Når sjekken er kjørt, går den nye teksten gjennom som vanlig.
4. **bygg** – lager siden i `site/` av det som er publiserbart. Feiler byggingen, publiseres ingenting.
5. **publiser** – committer `site/` til grenen `nettside` og pusher den, og statichost.eu publiserer den (se `PUBLISER_TIL` i `tingposten/config.py`), men **bare hvis filen `PUBLISER` finnes** i repo-roten. Uten den blir alt liggende lokalt.

## Publisering på statichost.eu

Siden bygges lokalt (eller på Pi-en), og `publiser` committer den ferdige siden til grenen `nettside` og pusher den. Etterpå ber `publiser` statichost om å publisere grenen (en POST til `https://builder.statichost.eu/tingposten`, med API-nøkkelen fra `.env`), så det trengs ingen webhook hos GitHub. Koden på `main` pushes ikke.

Gratisplanen (Hobby) gir ett nettsted, 10 GB trafikk og eget domene. Siden er liten (rundt 7 KB per side), så det holder lenge. Serverne står i Stockholm og eies av et europeisk selskap.

Oppsett, én gang:

1. **statichost:** lag nettstedet, koble det til repoet `v3x4t0r/tingposten` og velg grenen **`nettside`** (ikke `main`). Byggeoppsett trengs ikke: grenen har en `statichost.yml` med `public: .`, som betyr «publiser filene som de er».
2. **statichost → Settings → Automatic deployment:** slå på «Enforce authentication». Da kan bare den som har API-nøkkelen (`STATICHOST_APIKEY` i `.env`) starte en publisering. En GitHub-webhook trengs ikke, og den ville uansett ikke virket med autentisering på, siden GitHub ikke kan sende nøkkelen.
3. Grenen heter `nettside`, ikke `gh-pages`: så lenge det finnes en `gh-pages`-gren, holder GitHub Pages siden ute, og da ligger den ute to steder.
4. Test: `uv run tingposten bygg && uv run tingposten publiser`. Siden ligger da på `https://<navnet>.statichost.page/` (Tingposten: https://tingposten.statichost.page/).
5. Eget domene: tingposten.no peker på `tingposten.statichost.page.` (ALIAS for rotdomenet, CNAME for www), og domenet er lagt inn under Settings → Domains hos statichost. www sendes videre til tingposten.no. Se statichost.eu/docs/domains/.

Vil du heller laste opp direkte uten git, sett `PUBLISER_TIL = "statichost-upload"` i `tingposten/config.py` og legg en API-nøkkel i `.env` (`STATICHOST_APIKEY`).

## PDF-er for hånd

Så lenge automatisk henting er av, laster du ned PDF-ene selv. Det er lite: rundt 100 filer i måneden (ett bystyremøte og 4–5 byrådsmøter), altså et par minutter i uka.

```sh
bash ~/tingposten/data/bergen/last-ned-mangler.sh
```

Skriptet lages på nytt hver natt. Det henter én fil om gangen med 5 s pause og identifiserbar User-Agent. Filer som alt finnes hoppes over, så et avbrutt løp kan bare startes på nytt. Svarer serveren med feil, stopper skriptet med en gang. Kjør det fra hjemmenettet, ikke over VPN. Neste nattlige kjøring skriver kortene. Skriptet finnes bare når noe mangler.

For å lage det med en gang: `uv run tingposten pdf-urler --mangler --script data/bergen/last-ned-mangler.sh`.

## Kontrollpanel

`tingposten admin` er et lite nettpanel på Pi-en, til bruk fra mobil eller PC hjemme: `http://<pi-navn>.local:8787/`. Det viser

- **neste protokoll**: hvilke møter som venter på protokoll, og når den er ventet. Anslaget bygger på datoene i tidligere protokoller (bystyret: 1–5 dager etter møtet; byrådet: samme dag);
- **møtene** de siste tre månedene og hvor hvert står (kommer, venter, klar til nedlasting, ferdig);
- **siden**: sist publisert og bygget, antall saker, kort, artikler og rettelser, om daglig publisering er på og om STOP er satt, og en lenke til besøkstallene;
- **maskinen**: ledig disk, om `claude` og `codex` finnes, og loggen fra siste kjøring.

To knapper:

- **Last ned og publiser** (trykk to ganger) kjører `deploy/run-daily.sh --manuell`: henter agendaen, laster ned PDF-ene som mangler med det samme skriptet du ellers kjører for hånd (én fil om gangen, identifiserbar), skriver og faktasjekker, bygger og publiserer. Knappen trenger ikke `PUBLISER`-filen, men STOP stopper fortsatt publiseringen.
- **Sjekk nå** kjører `run-daily.sh --sjekk`: henter bare agendaen, så panelet viser om Bergen har lagt ut noe nytt.

Ingenting hentes eller publiseres før du trykker, og den daglige kjøringen går som før. Det går bare én kjøring om gangen (samme lås som cron). En kjøring startet for hånd hopper over agendahentingen hvis den ble gjort for under 30 minutter siden, så kommunens server ikke får dobbelt så mange forespørsler.

Panelet krever passord når det lytter på nettverket: `TINGPOSTEN_ADMIN_PASSORD` i `.env` (`setup-pi.sh` lager et tilfeldig). Brukernavnet kan være hva som helst. Det svarer bare på lokale navn og IP-adresser. Åpne ikke port 8787 i ruteren: panelet er laget for hjemmenettet, og passordet går ukryptert over HTTP.

Installer det som en tjeneste som starter med Pi-en (`setup-pi.sh` skriver ut linjene med riktige stier):

```sh
sed 's|<user>|pi|g' deploy/tingposten-admin.service | sudo tee /etc/systemd/system/tingposten-admin.service
sudo systemctl daemon-reload && sudo systemctl enable --now tingposten-admin
journalctl -u tingposten-admin -f     # panelets egen logg
```

Uten sudo, som brukertjeneste (krever at «linger» er på for brukeren, `loginctl show-user $USER -p Linger`; slik kjører den på Pi-en vår):

```sh
mkdir -p ~/.config/systemd/user
sed -e '/^User=/d' -e '/network-online/d' -e 's|WantedBy=multi-user.target|WantedBy=default.target|' \
    -e "s|/home/<user>|$HOME|g" deploy/tingposten-admin.service > ~/.config/systemd/user/tingposten-admin.service
systemctl --user daemon-reload && systemctl --user enable --now tingposten-admin
journalctl --user -u tingposten-admin -f
```

Etter `git pull`: `systemctl --user restart tingposten-admin` (eller `sudo systemctl restart tingposten-admin`).

Lokalt, uten passord: `uv run tingposten admin` og åpne http://127.0.0.1:8787/.

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

## Ny artikkelstruktur fra 30.10.2026: hva som skjer med gamle kort

Skribentreglene er versjon `skribent-3` (ingress, ordgrenser, brødtekst med roller). Kort og artikler skrevet under eldre regler **fryses** (`pipeline.eldre_regler`): de skrives ikke på nytt bare fordi reglene fikk nytt versjonsnummer, redigeres ikke på nytt, og sendes ikke til omskriving. Det koster null modellkall, og siden viser gamle og nye saker om hverandre. Bare nye saker, og saker der PDF-ene endrer seg, får `skribent-3`.

Vil du likevel ha et gammelt kort i ny form, slett `data/bergen/kort/<nøkkel>.json` (og `artikler/<nøkkel>.json`) og kjør `kjor`; til den nye sjekken er kjørt, beholdes det publiserte kortet (punkt 3 over). Les saken i `site/` før du publiserer: ingress uten «Bystyret …», status i klarspråk, mellomtitler i brødteksten.

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

## Rettelser

Er noe feil på en sak, retter du det med `tingposten rett`. Saken får da en synlig merknad rett under toppen, merket «Rettet», med datoen og det du skrev om hva som ble endret, slik den redaksjonelle erklæringen lover. Feltet som ble endret, får også et lite «Rettet», det samme gjør saken i listene, og i RSS begynner saken med merknaden.

```sh
cd ~/tingposten
# Bytt ut teksten i et felt:
uv run --no-sync tingposten rett https://tingposten.no/bergen/sak/571424/2026-09-23/266-26/ \
  --merknad "Vi skrev at 45 stemte for. Riktig er 54." \
  --felt status --tekst "Vedtatt med 54 mot 13 stemmer."
# Bare en merknad, uten å endre tekst:
uv run --no-sync tingposten rett 571424/2026-09-23/266-26 --merknad "…"
# Vis den:
uv run --no-sync tingposten bygg && uv run --no-sync tingposten publiser
```

- Saken kan oppgis som adressen, stien (`bergen/sak/571424/2026-09-23/266-26`) eller nøkkelen (`571424/2026-09-23/266-26`).
- `--felt` er `overskrift`, `ingress`, `hva_endres`, `hvem_pavirkes`, `hvorfor`, `status`, `nar_og_hvor` eller `artikkel:<n>` (avsnitt n i brødteksten). På eldre kort uten ingress er ingressen første setning i `hva_endres`: rett `hva_endres`, så følger den med. `--tekst ''` fjerner teksten.
- Feltet beholder kildene sine. Gjelder den nye teksten andre sider, gi dem med `--kilde saksframlegg:5` (kan gjentas).
- Merknaden vises slik du skriver den: hva som var feil og hva som er riktig, i én eller to setninger.
- Kommandoen sjekker at feltet finnes på saken, lagrer rettelsen med dagens dato og skriver den ut. Den bygger og publiserer ikke. Den nattlige kjøringen tar den med; `bygg` og `publiser` for hånd viser den med en gang.

Rettelsene ligger i `data/bergen/rettelser/<utvalg>/<dato>/<sak>.json`, ikke i kortene, og brukes når siden bygges. Derfor overlever de at pipelinen skriver kortet på nytt. `data/` er ikke i git, så kjør `rett` på maskinen som bygger siden (Pi-en). Har pipelinen skrevet om teksten en rettelse gjaldt, vises den nye teksten under merknaden, og byggeloggen får en advarsel («endret siden rettelsen» eller «finnes ikke på saken»). Se da over saken, og rett på nytt hvis feilen fortsatt er der. Merknaden blir stående uansett.

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
