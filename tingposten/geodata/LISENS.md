# Bydelsgrenser for Bergen

`bergen_bydeler.json` er avledet fra OpenStreetMap.

- **Kilde:** OpenStreetMap, administrative grenser (`boundary=administrative`, `admin_level=9`) innenfor Bergen kommune (`ref=4601`), hentet én gang via Overpass API 29. oktober 2026. Relasjons-ID-ene står i filens felt `kilde`.
- **Lisens:** Open Database License (ODbL) 1.0, <https://opendatacommons.org/licenses/odbl/1-0/>.
- **Kreditering:** «© OpenStreetMap-bidragsytere». Kartene på Tingposten viser denne kredittlinjen under hver figur.
- **Bearbeiding:** ytre ringer satt sammen fra OSM-veiene, forenklet med Douglas–Peucker (~250 m), øyer under ~0,2 km² fjernet, koordinater avrundet til fire desimaler. Grensene følger OSM og går derfor delvis ut i sjøen. Filen er en avledet database under samme lisens (ODbL «share-alike»).

Filen skal ikke oppdateres automatisk. Hvis bydelsgrensene endres, hentes de på nytt for hånd med én forespørsel, og denne filen oppdateres.
