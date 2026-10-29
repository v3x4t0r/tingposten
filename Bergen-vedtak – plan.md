# Bergen-vedtak – plan

Oct 29, 2026 · @Henrik

En gratis nettside som viser hva Bergen kommune faktisk vedtar, uten politisk prat: hver sak i samme lesbare format, de viktigste først, alt dokumentert med kilde.

## Mål og prinsipper

Siden skal gjøre det mulig for en vanlig innbygger å se hva som er vedtatt, hvem det gjelder og hvorfor – på under ett minutt per sak.

- **Fullstendighet:** alle saker fra organene vi dekker vises. Scoren bestemmer rekkefølgen, ikke hva som er med.
- **Sporbarhet:** hver setning lenker til et dokument. Ingen kilde, ingen setning.
- **Nøytralitet:** vi gjengir hva som ble foreslått, hva som ble vedtatt og av hvem. Vi vurderer ikke om det er bra. Redaksjonell erklæring publiseres på siden.
- **Personvern:** navn på privatpersoner (klagesaker, naboer) tas ikke med, selv om dokumentene er offentlige.
- **Kort:** harde lengdegrenser, aldri «artikkel for artikkelens skyld».

## Produktet

**Forside:** vedtak fra de siste 14 dagene, sortert etter påvirkningsscore. Under de fremhevede sakene står en linje «og N andre vedtak i perioden» som åpner hele listen. Vekslere for 30 dager og «i år».

**Møtesider (arkivet):** én side per møte: «Bystyret 29. oktober – 23 saker, 7 fremhevet», deretter alle kortene. Dette er beviset på at vi dekker alt.

**Kort (alle saker):** samme format hver gang.

| Felt | Innhold | Kilde |
| --- | --- | --- |
| Hva endres | Maks 3 setninger. Hvilket problem løses, hvis dokumentet sier det. | Saksframlegg |
| Hvem påvirkes | Grupper, områder, antall hvis oppgitt | Saksframlegg |
| Hvorfor | Kun byrådets/administrasjonens egen begrunnelse | Saksframlegg |
| Status | Vedtatt / under behandling / på høring, og stemmetall (f.eks. 39–28) | Protokoll |
| Når og hvor | Ikrafttredelse, bydel, adresse | Begge |
| Kilde | Lenke til saksframlegg og protokoll | API |

**Artikkel (kun over terskel):** maks 250 ord, maks 5 avsnitt. Legger bare til det kortet ikke har: bakgrunn, forkastede alternativer, hva som skjer videre, hvem som argumenterte for hva. Egen seksjon «Bakgrunn fra andre kilder» for alt som ikke kommer fra kommunen.

**Senere versjoner:** bydelsfilter og kart, åpne høringer med frist, stemmehistorikk per parti, oppfølgingssporing («byrådet bes komme tilbake innen …»), tidslinje per tema.

## Påvirkningsscore

Scoren (0–100) er forklarbar: de tre viktigste grunnene vises som små merker på kortet, f.eks. «47 MNOK · hele Åsane · 39–28 i bystyret». Leseren skal kunne være uenig i rekkefølgen og se hvorfor.

| Signal | Hentes fra | Trenger modell? |
| --- | --- | --- |
| Beløp (kroner i vedtak eller budsjettlinje) | Saksframlegg | Nei |
| Rekkevidde (antall berørte, areal, hel bydel vs ett gnr/bnr) | Saksframlegg | Nei |
| Juridisk vekt (endrer forskrift eller kommuneplan = høy; enkelt byggesak = lav) | Tittel + saksframlegg | Nei |
| Uenighet (ikke enstemmig, eller utvalget endret byrådets forslag) | Protokoll | Nei |
| Endelighet (bystyret > byrådet alene > utvalgsinnstilling) | utvalgId | Nei |
| Reversibilitet (engangstilskudd vs 20-års reguleringsplan) | Saksframlegg | Delvis |
| «Merker en vanlig innbygger dette i hverdagen?» 1–5, med én setnings begrunnelse vist på kortet | Modell | Ja |

Terskel for artikkel settes etter at de første 3–4 møtene er kjørt, slik at ca. 20–30 % av sakene får artikkel. Vektene justeres manuelt de første månedene.

## Datalag

Bergen kommune kjører Public 360 («bk360») med et åpent JSON-API bak saksøket. Ingen scraping nødvendig. Organ-ID-er og tier-inndeling står i [Bergen politiske organer – kart](https://claude.ai/artifact/MVzX5PQ8DiX5dQq9Btpg6D).

| Endepunkt (`https://www.bergen.kommune.no/politikere-utvalg/api/…`) | Gir | Brukes til |
| --- | --- | --- |
| `agendapunkter?start=0&rows=N` | Nyeste agendapunkter først, med `numTotal` | Daglig henting av alt nytt |
| `agendapunkter/{kildeId}` | Ett agendapunkt | Oppslag |
| `saksgang?dokumentnummer=…` | Alle møter en sak har vært gjennom | Tidslinje per sak |
| `agendapunktfiler?dokumentnummer=…` | Vedlegg (kart, rapporter, brev) | Bakgrunn |
| `fil/bk360/{filId}/{slug}` | PDF | Saksframlegg og protokoll |
| `utvalgForPolitiskeSaker` | Alle 111 organer med ID | Organliste, hentes ved hver kjøring |

**Nøkkelfelt per agendapunkt:** `saksnr` (kobler samme sak på tvers av organer og år), `utvalgId`, `moteId`, `moteDato`, `kategori`, `referansenr` (saksnummer i møtet, f.eks. 372/26), `innstillingFilId` (saksframlegg), `vedtakFilId` (protokoll/vedtak), `tag` (publiseringsdato).

**Endringsdeteksjon:** «noe er vedtatt» = `vedtakFilId` går fra `null` til en ID. Lagre alle agendapunkter lokalt og sammenlign ved hver kjøring.

**Støyfilter på tittel (før noen modell kjøres):** innkalling, godkjenning av protokoll, muntlige spørsmål, skriftlige spørsmål, saker til gjennomsyn, orienteringssaker. Fjerner ca. en tredjedel av punktene.

**PDF-ene** er generert, ikke skannet. Protokollene har fast struktur per sak: saksnummer, tittel, Behandling, Votering, Vedtak. Parse med pymupdf/pdfplumber; kun prosatekst sendes til modell.

**Lovdata:** brukes målrettet, ikke som generell kryssjekk. Hent hjemler nevnt i saksframlegget (f.eks. plan- og bygningsloven § 12-12) og lenk til paragrafen. Når en sak endrer en lokal forskrift, vis før/etter.

## Pipeline

Kjøres daglig. Hvert steg er en egen rolle med egen instruks; ingen rolle ser forrige rolles resonnement, bare resultatet.

1. **Innhenter** (skript, ingen modell): henter nye og endrede agendapunkter, laster ned saksframlegg og protokoll, parser PDF til tekst, lagrer alt lokalt.
2. **Klassifiserer**: tittelfilter først (gratis). Deretter modell: relevant for innbyggere ja/nei, kategori (regulering, skole, helse, budsjett, forskrift …), bydel, og signalene til påvirkningsscoren.
3. **Skribent**: skriver kortet, og artikkel hvis scoren er over terskel. Hver setning må ha referanse til side/avsnitt i et kildedokument. Får 5 håndskrevne eksempler som mal.
4. **Faktasjekker**: egen modell som bare får kildedokumentene og teksten. Fjerner – ikke omformulerer – enhver setning som ikke er dekket av kilden, og enhver setning som gjentar kortet.
5. **Redaktør** (kun sletting): håndhever lengdegrenser og forbudslisten. Kan ikke legge til eller omskrive, bare fjerne.
6. **Publiserer** (skript): skriver markdown, bygger HTML med Jinja2, pusher til \`gh-pages\`. Hopper over hvis kill switch er satt.

Et steg som bare kan slette, kan ikke blåse opp teksten. Det er hele poenget med steg 5.

## Redaksjonelle regler mot oppblåst tekst

1. **Artikkelen gjentar aldri kortet.** Test: inneholder setningen et faktum som ikke allerede står på kortet? Hvis ikke, slettes den.
2. **Harde grenser, ikke veiledning.** Kort: maks 3 setninger per felt. Artikkel: maks 250 ord, maks 5 avsnitt. Over grensen → avvist og skrevet på nytt, ikke trimmet.
3. **Hver setning har kilde.** Setning uten referanse fjernes, ikke mykes opp. Dette tar automatisk livet av «dette vedtaket er et viktig skritt for byen».
4. **Forbudsliste:** innledninger («I dette vedtaket …»), oppsummeringer som gjentar, «det er viktig å merke seg», adjektiver om viktighet (scoren viser viktighet), retoriske spørsmål, punktlister inne i artikkelen.
5. **Siste steg kan bare slette.** Ingen tillegg, ingen omformulering.
6. **Mål det.** Logg ord per artikkel og setninger per nytt faktum. Stiger snittet, har noe sklidd.

Skriv 5 artikler for hånd først. De blir standarden og eksemplene skribenten får. Modeller kopierer lengde og tone fra eksempler langt mer pålitelig enn fra instrukser.

## Teknisk oppsett

Skript + statisk side, alt kjører på en Raspberry Pi hjemme og pushes ferdig bygget til GitHub Pages. Ingen GitHub Actions-minutter, ingen server, ingen database.

| Del | Valg | Merknad |
| --- | --- | --- |
| Navn og domene | Tingposten, `tingposten.no` | Bergen under `/bergen/`; senere `/stortinget/` og andre byer som nye mapper |
| Repo | `midttun/tingposten` | Offentlig. Pages serverer `gh-pages`-grenen med `CNAME`-fil |
| Kjøring | Raspberry Pi 5 (16 GB, deler maskin med Home Assistant, Jellyfin, Navidrome), daglig cron | Pipelinen er lett (API-kall og PDF-parsing). Kjør i egen Docker-container eller venv for å holde den adskilt. Egen Pi (8 GB) bare hvis det blir trangt. |
| Pipeline | Python 3.11+ | `requests` mot API, `pymupdf` for PDF, JSON-filer som lager |
| Skribent | Claude (Anthropic API) | Klassifisering på liten modell, kort og artikkel på sterk modell |
| Faktasjekker/redaktør | Codex (OpenAI API) | Tilpasses fra review-pipelinen i freetime.no (dev). Annen leverandør enn skribenten med vilje. |
| Sidegenerator | Python + Jinja2 + håndskrevet CSS | Ingen Hugo/Astro. Maler i repoet, `build.py` lager HTML |
| Publisering | Helautomatisk | Ingen manuell godkjenning. Pi bygger og `git push` til `gh-pages` |
| Sikkerhetsnett | «Meld feil»-lenke på hvert kort + kill switch | Kill switch: finnes filen `STOP` i repoet, hoppes publisering over |
| Språk | Bokmål, klarspråk i tredje person | Ikke «du». Tone settes av 5 håndskrevne eksempler |
| Distribusjon | Ingen i POC | RSS er én mal til når ønsket; e-post senere |

To forbehold med Pages og Actions:

- API-nøkler ligger i `.env` på Pi-en, aldri i repoet.
- Repoet er offentlig. Det er fint – åpen kode styrker tilliten – men ingen uttrekk med personnavn må committes. Rens før lagring, ikke etter.
- Helautomatisk publisering betyr at faktasjekkeren er eneste vern før noe blir offentlig. Første uke: les alt selv i etterkant og juster instruksene.

## Faser

**Fase 0 – grunnlag (1–2 dager)**

- [ ] Repo med Pages og Actions satt opp, tom side publisert
- [ ] Innhenter: hent alle agendapunkter for de tre Tier 1-organene i 2026, lagre som JSON
- [ ] Last ned og parse saksframlegg + protokoll for ett bystyremøte
- [ ] Skriv 5 kort og 5 artikler for hånd fra det møtet (standarden)
- [ ] Send e-post til Bystyrets kontor: hva vi bygger, spør om ønsket hentefrekvens

**Fase 1 – MVP (1–2 uker)**

- [ ] Tittelfilter + klassifiserer
- [ ] Skribent → faktasjekker → redaktør, med lengdegrenser og forbudsliste
- [ ] Påvirkningsscore med synlige grunner på kortet
- [ ] Forside (14 dager, etter score) og møtesider
- [ ] RSS og e-postliste
- [ ] Redaksjonell erklæring publisert
- [ ] Kjør 3–4 møter, juster terskel og vekter, les alt selv

**Fase 2 – nyttig for den enkelte (uke 3–6)**

- [ ] Tier 2-organene (de fire utvalgene)
- [ ] Bydelsfilter og kart
- [ ] Åpne høringer med frist
- [ ] Stemmetall per parti, automatisk partiside
- [ ] Hjemler lenket til Lovdata; før/etter ved forskriftsendring

**Fase 3 – ansvarliggjøring (måned 2–4)**

- [ ] Kontrollutvalget og de rådgivende rådene
- [ ] Oppfølgingssporing: trekk ut «bes komme tilbake innen …», vis forfalte
- [ ] Tidslinje per sak via `saksnr` og periode-ID-kobling
- [ ] Logg og dashboard for ord per artikkel og feil funnet av faktasjekker

**Senere, kun ved trekk:** korrelasjoner på tvers av saker, andre kommuner på Public 360, database og brukerkontoer («følg min bydel»).

## Risiko og åpne spørsmål

| Risiko | Tiltak |
| --- | --- |
| Feil i sammendrag av et vedtak som noen deler videre | Faktasjekker med egen modell; kilde per setning; «meld feil»-lenke på hvert kort |
| robots.txt forbyr automatisk henting av filene | Avklar med Bystyrets kontor; lav frekvens (én kjøring daglig); identifiserbar User-Agent med kontaktadresse |
| Personnavn i klagesaker havner i offentlig repo | Rens tekstuttrekk før lagring; privatpersoners navn aldri i kort eller artikkel |
| Opplevd skjevhet («dere fremhever alltid X») | Forklarbar score; redaksjonell erklæring; vekter publisert |
| Organ-ID-er endres ved valget 2027 | Organliste hentes fra API ved hver kjøring; koblingstabell for perioder |
| Tekst sklir mot oppblåsthet over tid | Logg ord/artikkel; slette-bare-steg; eksempler som mal |

**Åpne spørsmål**

- [ ] Støtter `agendapunkter` filter på `utvalgId`, år/måned og fritekst? (Sjekk i Network-fanen ved å velge et utvalg i søket.)
- [ ] Hvor raskt etter møtet legges protokollen ut? (Avgjør når daglig kjøring bør gå.)
- [ ] Navn og domene for siden.

## Overlevering til Claude Code

Alt over er besluttet og kan bygges som det står. Start med fase 0. Disse tre tingene må gis i oppstartsmeldingen, fordi de ikke kan utledes fra planen:

- [ ] Sti til review-pipelinen i freetime.no (repo og mappe) som faktasjekker-steget skal tilpasses fra
- [ ] Hvor «meld feil» skal gå: GitHub-issue-lenke (null oppsett) eller `mailto:` til en adresse på tingposten.no
- [ ] Klokkeslett for daglig kjøring (forslag: 06:00, før folk leser nyheter)

Foreslått første melding:

```markdown
Bygg Tingposten etter planen i dette dokumentet, fase 0 først.
Repo: midttun/tingposten. Kjører på Raspberry Pi 5, Python 3.11+, Jinja2, pushes til gh-pages.
Skribent: Claude. Faktasjekker: Codex – kopier mønsteret fra <sti til freetime.no-review>.
Start med innhenteren mot /politikere-utvalg/api/agendapunkter for utvalgId 571424, 900101 og 900102.
Ikke bygg sidemaler før innhenteren har hentet ett helt bystyremøte og parset protokollen.
```
