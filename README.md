# Tingposten

Hva Bergen kommune faktisk vedtar – hver sak i samme lesbare format, de viktigste først, alt dokumentert med kilde.

Planen står i [`Bergen-vedtak – plan.md`](Bergen-vedtak%20–%20plan.md).

## Kjøre lokalt

```sh
uv sync
cp .env.example .env    # fyll inn
uv run tingposten hent                 # i år, Tier 1-organene
uv run tingposten hent --year 2026 --utvalg 571424
uv run pytest
```

Data lagres i `data/bergen/` (ikke i git):

- `utvalg.json` – alle politiske organer, hentes ved hver kjøring
- `agendapunkter/<utvalgId>.json` – rå agendapunkter fra API-et, nøkkel `id`
- `endringer.jsonl` – én linje per endring: `ny`, `vedtatt`, `endret`, `forsvunnet`

Innhenteren venter 5 sekunder mellom forespørsler og gir opp kjøringen hvis serveren nekter gjentatte ganger. Bergen kommunes brannmur sperrer IP-en ved for mange kall på kort tid.
