# Debatt: kommentarserveren på Pi-en

Kommentarene ligger på Pi-en, ikke hos statichost. Den statiske siden henter dem med et lite skript (`tingposten/static/debatt.js`) fra kommentarserveren, som leserne når gjennom **Tailscale Funnel** på `https://debatt.<tailnet>.ts.net`. Ingen porter åpnes i ruteren, og hjemme-IP-en er ikke synlig.

Serveren kjører i egne containere, som en egen Tailscale-maskin («debatt»), adskilt fra resten av Pi-en: den ser bare `data/debatt/` (kommentarbasen og saksoversikten som `bygg` skriver) og sin egen `debatt.env`. Den har ikke tilgang til `.env`, deploy-nøkkelen, Claude-innloggingen, Home Assistant eller kontrollpanelet.

Alt er av til du slår det på: `DEBATT = None` i `tingposten/config.py` betyr at siden ikke viser kommentarfeltet.

## Velg moderator først

Kjør testen før noe annet. `tingposten debatt-fasit` er en lokal side der du setter fasit på de 150 testkommentarene i `data/debatt/testsett.jsonl`. Forslagene er ferdig utfylt; du retter bare der du er uenig. Deretter:

```sh
uv run --extra debatt tingposten debatt-test --moderator hailo      # Hailo-10H, ~6 s per kommentar
uv run --extra debatt tingposten debatt-test --moderator cpu        # llama.cpp på CPU, ~25 s
uv run --extra debatt tingposten debatt-test --moderator claude     # trenger TINGPOSTEN_ANTHROPIC_API_KEY
uv run --extra debatt tingposten debatt-test --moderator jev        # trenger TYPESAFE_API_KEY
uv run --extra debatt tingposten debatt-test --rapport              # tabellen for de nyeste kjøringene
```

Det viktigste i tabellen er **farlige slipp** (trussel, hets, privatperson eller beskyldning som ble publisert; skal være 0), **feil stoppet** (lovlige meninger som ble stoppet; nøytralitetsmålet) og hvor mange som havner hos deg. Sett så `DEBATT_MODERATOR` og `DEBATT_TERSKEL` i `tingposten/config.py`. Moderatorene spørres i rekkefølge; den neste spørres bare når den forrige er usikker, og er alle usikre, venter kommentaren på deg.

## Oppsett, én gang

1. **Tailscale** (admin.tailscale.com):
   - Slå på HTTPS-sertifikater (DNS → HTTPS Certificates) og Funnel.
   - Lag en tagg `tag:debatt`, og gi den Funnel i tilgangsreglene:
     ```json
     "tagOwners": {"tag:debatt": ["autogroup:admin"]},
     "nodeAttrs": [{"target": ["tag:debatt"], "attr": ["funnel"]}]
     ```
     Gi ikke `tag:debatt` tilgang til andre maskiner i tailnettet (ingen regel med den som kilde).
   - Lag en auth key (Settings → Keys): gjenbrukbar nei, forhåndsgodkjent ja, tagg `tag:debatt`.
   - Skriv den i `deploy/debatt/tailscale.env`: `TS_AUTHKEY=tskey-auth-…`
2. **E-post:** kopier `debatt.env.example` til `debatt.env` og fyll inn SMTP for freetime.no-kontoen (Domeneshop: `smtp.domeneshop.no`, port 587, brukernavn og passord fra Domeneshop). Avsender kan være `tingposten@freetime.no` eller en egen adresse.
3. **Moderatoren:**
   - **hailo:** hailo-ollama må lytte der containeren når den, på Docker-broen. Lag en drop-in (krever sudo):
     ```sh
     sudo systemctl edit hailo-ollama      # legg inn:
     [Service]
     Environment=OLLAMA_HOST=172.17.0.1:8000
     sudo systemctl enable --now hailo-ollama
     ```
     Modellen (`qwen3:1.7b`) er alt lastet ned under `~/.local/share/hailo-ollama/`. Tjenesten kjører som root og bruker `/root/.local/share/hailo-ollama/`; enten kjør `curl -X POST -H 'Content-Type: application/json' http://172.17.0.1:8000/api/pull -d '{"model":"qwen3:1.7b","stream":false}'` én gang, eller kjør den som din egen brukertjeneste i stedet.
   - **cpu:** legg `Qwen3.5-4B-Q4_0.gguf` i `~/tingposten/modeller/` og start med `--profile cpu` (under).
   - **claude / jev:** nøkkelen i `debatt.env`.
4. **Start:**
   ```sh
   cd ~/tingposten/deploy/debatt
   docker compose up -d --build                 # med cpu-moderatoren: docker compose --profile cpu up -d --build
   docker compose logs -f debatt
   ```
   Adressen står i Tailscale-adminen under maskinen «debatt» (`https://debatt.<tailnet>.ts.net`). Test fra mobilen på mobilnett: `https://debatt.<tailnet>.ts.net/api/helse` skal svare `{"ok":true}`.
5. **Slå på kommentarfeltet:** sett `DEBATT = "https://debatt.<tailnet>.ts.net/"` i `tingposten/config.py`, og bygg og publiser siden. `bygg` skriver da også `data/debatt/saker.json`, som serveren leser når filen endres.

## Daglig

- Kommentarer som modellen er usikker på, og rapporterte kommentarer, står under **Debatt** i kontrollpanelet (`tingposten admin`). Der kan du publisere, fjerne eller fjerne og sperre forfatteren. Panelet viser navnet, ikke e-postadressen.
- Serveren tåler at Pi-en eller nettet er nede: siden virker som før, og kommentarfeltet sier at det er utilgjengelig.
- Sikkerhetskopi: `data/debatt/debatt.db` (SQLite). `sqlite3 data/debatt/debatt.db ".backup debatt-$(date +%F).db"` mens serveren kjører.
- Slå av: `docker compose down` og `DEBATT = None`.
