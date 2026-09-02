# zoeck.com/weer

Actuele KNMI-waarnemingen als statische pagina. Een GitHub Action haalt elke
10 minuten de laatste 24 uur per station op uit de KNMI EDR API, schrijft
`site/data.json` en uploadt de map `site/` via FTP(S) naar `/weer/`.
De pagina zelf doet geen API-calls en bevat geen keys.

```
fetch_weather.py            ophalen + data.json schrijven
site/index.html             frontend (leest alleen data.json)
.github/workflows/weer.yml  planning + FTP-upload
```

## Installatie

1. Maak een account op https://developer.dataplatform.knmi.nl en vraag een
   API-key aan voor de **EDR API** (een geregistreerde key heeft ruimere
   limieten dan de anonieme key).
2. Zet deze map in een nieuwe GitHub-repository en push naar `main`.
   Een publieke repo heeft onbeperkte Actions-minuten; bij een private repo
   kost 144 runs/dag meer dan het gratis quotum — zet de cron dan op `*/20`.
3. Voeg in de repo onder *Settings → Secrets and variables → Actions* toe:
   - `KNMI_API_KEY`
   - `FTP_SERVER`, `FTP_USERNAME`, `FTP_PASSWORD`
4. Controleer `server-dir` in `.github/workflows/weer.yml` — dit moet het pad
   zijn waarin `zoeck.com/weer/` terechtkomt (bijv. `/public_html/weer/`).
   Zet `protocol` op `ftp` als je host geen FTPS ondersteunt.
5. Start de workflow één keer handmatig via *Actions → Weer bijwerken → Run
   workflow*. Daarna draait hij elke 10 minuten.

## Lokaal testen

```
export KNMI_API_KEY=...
python fetch_weather.py
cd site && python -m http.server 8000     # open http://localhost:8000
```

## Aanpassen

- Stations: lijst `STATIONS` in `fetch_weather.py` (het eerste station is de
  standaardkeuze). Alle ids en namen: `GET …/collections/10-minute-in-situ-meteorological-observations/locations`.
- Variabelen: lijst `PARAMS`; de namen staan in de KNMI-documentatie van de
  dataset. Voeg je er een toe, voeg dan ook een regel toe aan `facts` in
  `index.html`.
- Een station kiezen via URL: `zoeck.com/weer/#0-20000-0-06260`.

## Bekende beperkingen

- GitHub start geplande workflows soms enkele minuten later; de pagina toont
  de tijd van de waarneming, en markeert waarnemingen ouder dan een uur.
- De 10-minutenwaarnemingen zijn ongevalideerd; KNMI kan ze tot 7 dagen later
  nog aanvullen.
- Geen verwachting — dat is fase 2 (HARMONIE via de Open Data API).
