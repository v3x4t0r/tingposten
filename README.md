# Tingposten

Hva Bergen kommune faktisk vedtar – hver sak i samme lesbare format, de viktigste først, alt dokumentert med kilde.

Nettstedet: <https://tingposten.no/>. Redaksjonell erklæring: [`docs/redaksjonell-erklaering.md`](docs/redaksjonell-erklaering.md) (publisert på Om-siden).

## Slik virker det

1. **hent** – agendapunktene fra Bergen kommunes åpne API (bk360), sakte og med en identifiserbar brukeragent.
2. **PDF-er** – saksframlegg og protokoll lastes foreløpig ned for hånd med et generert skript (`tingposten pdf-urler --mangler --script …`).
3. **kjor** – for hver avgjort sak: en språkmodell (Claude) skriver kortet, en annen (Codex) sjekker hver setning mot sidene i kommunens dokumenter, og redaktøren stryker det som ikke holder (den kan bare slette). De viktigste sakene får en kort bakgrunnsartikkel med egen faktasjekket tittel. Bare de siste tre månedene behandles.
4. **bygg** – den statiske nettsiden i `site/`, fra malene i `tingposten/templates/`.
5. **publiser** – committer `site/` til grenen `nettside` og ber statichost.eu publisere den.

Rettelser legges inn med `tingposten rett …` og vises på saken.

**Papirutgave** – `tingposten utgave` lager en trykkeklar PDF av ett møte, med det samme faktasjekkede innholdet som nettsiden: forside, hovedsakene i sin helhet, de andre sakene kort i sakslistens rekkefølge, og under hver sak kildene (dokument og side), en kortadresse (`tingposten.no/s/bs-266-26`) og en QR-kode. Kortadressene er regler i `_redirects` som `bygg` skriver, så de virker etter neste `bygg` + `publiser`. Trenger Chromium via Playwright (valgfri avhengighet):

```sh
uv run --extra utgave playwright install chromium   # én gang, hvis Playwright ikke finner en Chromium
uv run --extra utgave tingposten utgave --mote 571424-2026-09-23 --format tabloid --til utgave.pdf --png forhandsvisning/
```

Formater: `tabloid` (280 × 400 mm, 5 spalter, avispapir), `a4` (A4-hefte, 3 spalter) og `a5` (A5-hefte, 2 spalter; skriver også `<fil>-hefte.pdf` med to sider per liggende A4-ark i heftrekkefølge, for tosidig utskrift med vending på kortsiden). Hvert format får også en skjermkopi, `<fil>-skjerm.pdf`, med avispapirtone; trykkfilen er hvit. Daglig kjøring på en Raspberry Pi: [`deploy/README.md`](deploy/README.md).

## Kjøre lokalt

```sh
uv sync
cp .env.example .env                   # fyll inn
uv run pytest
uv run tingposten hent                 # agendapunkter for i år
uv run tingposten kjor                 # krever PDF-er og innlogget claude/codex
uv run tingposten bygg --til /tmp/tingposten && python3 -m http.server -d /tmp/tingposten
```

## Kode

- `tingposten/bk360.py`, `innhenter.py` – henting fra kommunens API
- `skribent.py`, `faktasjekker.py`, `klassifiserer.py`, `redaktor.py`, `artikkeltittel.py`, `pipeline.py` – kort og artikler
- `protokoll.py`, `saksframlegg.py`, `status.py`, `score.py`, `grafikk.py` – det som leses mekanisk ut av dokumentene: vedtak, stemmetall, bydeler, beløp, rangering og figurer
- `bygg.py`, `templates/`, `static/`, `typo.py`, `rettelser.py` – nettsiden
- `publiserer.py` – publisering
- `utgave.py`, `templates/utgave/` – papirutgaven

## Data

Alt ligger i `data/bergen/` og er ikke i git (dokumentene kan inneholde navn på privatpersoner):

- `utvalg.json` og `agendapunkter/<utvalgId>.json` – fra API-et
- `pdf/` – saksframlegg og protokoller
- `kort/`, `sjekk/`, `redigert/`, `artikler*/`, `artikkeltittel/` – utkast, faktasjekker og det som publiseres
- `rettelser/` – rettelser som legges på ved bygging
- `endringer.jsonl` – én linje per endring i agendapunktene

Innhenteren venter 5 sekunder mellom forespørsler og gir opp hvis serveren nekter gjentatte ganger. Bergen kommunes brannmur sperrer IP-en ved for mange kall på kort tid.
