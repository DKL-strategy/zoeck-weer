"""
Haalt de laatste 24 uur waarnemingen op uit de KNMI EDR API en schrijft
site/data.json. Draait elke 10 minuten via GitHub Actions; de frontend
(site/index.html) leest alleen dit JSON-bestand.

Env:  KNMI_API_KEY   (verplicht)
      KNMI_STATIONS  (optioneel, komma-gescheiden WIGOS-ids; overschrijft STATIONS)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

COLLECTION = "10-minute-in-situ-meteorological-observations"
BASE = f"https://api.dataplatform.knmi.nl/edr/v1/collections/{COLLECTION}"
OUT = Path(__file__).parent / "site" / "data.json"
HOURS = 24

# Eerste station is de standaardkeuze in de frontend. Volledige lijst: /locations
STATIONS = [
    "0-20000-0-06240",  # Schiphol
    "0-20000-0-06260",  # De Bilt
    "0-20000-0-06235",  # De Kooy
    "0-20000-0-06344",  # Rotterdam
    "0-20000-0-06280",  # Eelde
    "0-20000-0-06290",  # Twenthe
    "0-20000-0-06370",  # Eindhoven
    "0-20000-0-06310",  # Vlissingen
    "0-20000-0-06380",  # Maastricht
]

# Variabelen zoals gedocumenteerd voor deze collectie
PARAMS = ["ta", "td", "rh", "ff", "dd", "fx", "R1H", "R24H", "pp", "vv", "n", "ss", "qg", "rg"]


def headers():
    key = os.environ.get("KNMI_API_KEY")
    if not key:
        sys.exit("KNMI_API_KEY ontbreekt")
    return {"Authorization": key}


def get(path, **params):
    r = requests.get(f"{BASE}{path}", params=params, headers=headers(), timeout=40)
    r.raise_for_status()
    return r.json()


def station_index():
    feats = get("/locations")["features"]
    out = {}
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        out[f["id"]] = {"name": f["properties"].get("name", f["id"]), "lat": lat, "lon": lon}
    return out


def fetch_station(sid):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=HOURS)
    window = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
    try:
        cov = get(f"/locations/{sid}", datetime=window, **{"parameter-name": ",".join(PARAMS)})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            # station mist een van de parameters: haal alles en filter zelf
            cov = get(f"/locations/{sid}", datetime=window)
        else:
            raise

    times = cov["domain"]["axes"]["t"]["values"]
    series = {"t": times}
    for p in PARAMS:
        if p in cov.get("ranges", {}):
            series[p] = cov["ranges"][p]["values"]

    # laatste tijdstip met een geldige temperatuur (of anders laatste tijdstip)
    latest_idx = len(times) - 1
    for i in range(len(times) - 1, -1, -1):
        if series.get("ta", [None] * len(times))[i] is not None:
            latest_idx = i
            break
    latest = {p: v[latest_idx] for p, v in series.items() if p != "t"}
    latest_time = times[latest_idx]

    # 24-uurs min/max temperatuur uit de reeks
    temps = [v for v in series.get("ta", []) if v is not None]
    summary = {"tmin": min(temps), "tmax": max(temps)} if temps else {}

    return {"latest_time": latest_time, "latest": latest, "summary": summary, "series": series}


def main():
    previous = {}
    if OUT.exists():
        try:
            previous = {s["id"]: s for s in json.loads(OUT.read_text())["stations"]}
        except Exception:
            pass

    ids = os.environ.get("KNMI_STATIONS")
    ids = [s.strip() for s in ids.split(",")] if ids else STATIONS

    index = station_index()
    stations, errors = [], []
    for sid in ids:
        meta = index.get(sid, {"name": sid, "lat": None, "lon": None})
        try:
            data = fetch_station(sid)
            stations.append({"id": sid, **meta, **data, "error": None})
        except Exception as e:  # station overslaan, vorige data behouden
            errors.append(f"{sid}: {e}")
            if sid in previous:
                stations.append({**previous[sid], "error": str(e)})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "KNMI Data Platform, EDR API, " + COLLECTION,
        "hours": HOURS,
        "stations": stations,
    }, ensure_ascii=False, separators=(",", ":")))

    print(f"{len(stations)} stations geschreven naar {OUT}")
    for e in errors:
        print("fout:", e, file=sys.stderr)
    if not stations:
        sys.exit(1)


if __name__ == "__main__":
    main()
