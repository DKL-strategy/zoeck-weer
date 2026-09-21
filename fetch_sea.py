"""
Golven en getij van Rijkswaterstaat (WaterWebservices / WADAR) -> site/sea.json

- Zoekt in de catalogus de meetpunten bij Scheveningen, IJmuiden en Europlatform
- Golven: significante golfhoogte Hm0 (cm), golfperiode Tm02 (s), golfrichting Th0 (graden), afgelopen 24 uur
- Getij: waterhoogte WATHTE (cm t.o.v. NAP): gemeten (afgelopen 24 uur) + voorspeld (komende 48 uur),
  hoog- en laagwater berekend als extremen van de reeks
- Watertemperatuur T (compartiment OW) waar beschikbaar

Geen API-key nodig. Probeert eerst de nieuwe API (ddapi20), daarna de oude.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

OUT = Path(__file__).parent / "site" / "sea.json"
NEW = "https://ddapi20-waterwebservices.rijkswaterstaat.nl"
OLD = "https://waterwebservices.rijkswaterstaat.nl"
HEADERS = {"Content-Type": "application/json", "X-API-KEY": "zoeck-weer"}
# Doelplekken (strand): per grootheid wordt het dichtstbijzijnde meetpunt gezocht dat nu data levert
TARGETS = {"scheveningen": ("Scheveningen", 52.105, 4.270), "ijmuiden": ("IJmuiden", 52.465, 4.555)}
MAX_KM = 60
HOURS_BACK, HOURS_FWD = 24, 48


def post(base, path, body, timeout=90):
    r = requests.post(base + path, json=body, headers=HEADERS, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
    return r.json()


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def parse_t(s):
    # "2026-09-21T09:40:00.000+02:00" -> ISO UTC
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def catalogus(base):
    cat = post(base, "/METADATASERVICES/OphalenCatalogus" if base == NEW else "/METADATASERVICES_DBO/OphalenCatalogus/",
               {"CatalogusFilter": {"Compartimenten": True, "Grootheden": True, "Eenheden": True}})
    locs = {l["Locatie_MessageID"]: l for l in cat.get("LocatieLijst", [])}
    meta = {m.get("AquoMetadata_MessageID", m.get("AquoMetaData_MessageID")): m for m in cat.get("AquoMetadataLijst", [])}
    # koppeling: welke grootheden zijn er per locatie
    per_loc = {}
    for link in cat.get("AquoMetadataLocatieLijst", []):
        lid = link.get("Locatie_MessageID"); mid = link.get("AquoMetaData_MessageID", link.get("AquoMetadata_MessageID"))
        m = meta.get(mid)
        if lid in locs and m:
            per_loc.setdefault(lid, set()).add((m.get("Compartiment", {}).get("Code"), m.get("Grootheid", {}).get("Code")))
    return locs, per_loc


def hav(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0; p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def candidates(locs, per_loc, grootheid, lat, lon):
    """Locaties met deze grootheid, gesorteerd op afstand tot (lat, lon), binnen MAX_KM."""
    out = []
    for lid, l in locs.items():
        if ("OW", grootheid) not in per_loc.get(lid, set()):
            continue
        la, lo = latlon(l)
        if la is None:
            continue
        d = hav(lat, lon, la, lo)
        if d <= MAX_KM:
            out.append((d, l))
    out.sort(key=lambda x: x[0])
    return out


def series(base, loc, grootheid, start, end, proces=None):
    if base == NEW:
        body = {"Locatie": {"Code": loc["Code"]},
                "AquoPlusWaarnemingMetadata": {"AquoMetadata": {"Compartiment": {"Code": "OW"}, "Grootheid": {"Code": grootheid}}},
                "Periode": {"Begindatumtijd": ts(start), "Einddatumtijd": ts(end)}}
        if proces:
            body["AquoPlusWaarnemingMetadata"]["AquoMetadata"]["ProcesType"] = proces
        path = "/ONLINEWAARNEMINGENSERVICES/OphalenWaarnemingen"
    else:
        body = {"AquoPlusWaarnemingMetadata": {"AquoMetadata": {"Compartiment": {"Code": "OW"}, "Grootheid": {"Code": grootheid}}},
                "Locatie": {"X": loc["X"], "Y": loc["Y"], "Code": loc["Code"]},
                "Periode": {"Begindatumtijd": ts(start), "Einddatumtijd": ts(end)}}
        path = "/ONLINEWAARNEMINGENSERVICES_DBO/OphalenWaarnemingen"
    res = post(base, path, body)
    pts = {}
    for w in res.get("WaarnemingenLijst", []):
        for m in w.get("MetingenLijst", []):
            v = (m.get("Meetwaarde") or {}).get("Waarde_Numeriek")
            q = m.get("WaarnemingMetadata", {})
            qc = q.get("Kwaliteitswaardecode") or (q.get("KwaliteitswaardecodeLijst") or [None])[0]
            if v is None or v in (999999999, -999999999) or qc in ("99",):
                continue
            pts[parse_t(m["Tijdstip"])] = float(v)
    t = sorted(pts)
    return {"t": t, "v": [pts[x] for x in t]}


def extremes(t, v, min_gap_h=4):
    """Hoog- en laagwater: lokale maxima/minima met minimaal min_gap_h uur ertussen."""
    out = []
    for i in range(1, len(v) - 1):
        if v[i] >= v[i - 1] and v[i] > v[i + 1]:
            out.append({"t": t[i], "type": "HW", "h": v[i]})
        elif v[i] <= v[i - 1] and v[i] < v[i + 1]:
            out.append({"t": t[i], "type": "LW", "h": v[i]})
    # ruis wegfilteren: afwisselend HW/LW en minimaal gap; bij dubbele van dezelfde soort de extreemste houden
    clean = []
    for e in out:
        if clean and clean[-1]["type"] == e["type"]:
            if (e["type"] == "HW" and e["h"] > clean[-1]["h"]) or (e["type"] == "LW" and e["h"] < clean[-1]["h"]):
                clean[-1] = e
            continue
        if clean and (datetime.fromisoformat(e["t"].replace("Z", "+00:00")) - datetime.fromisoformat(clean[-1]["t"].replace("Z", "+00:00"))) < timedelta(hours=min_gap_h):
            continue
        clean.append(e)
    return clean


def has_recent(ser, hours=3):
    if not ser or not ser["t"]:
        return False
    last = datetime.fromisoformat(ser["t"][-1].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - last <= timedelta(hours=hours) and len(ser["t"]) >= 3


def build(base):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    locs, per_loc = catalogus(base)
    print(f"{base}: {len(locs)} locaties in catalogus")
    proces = "meting" if base == NEW else None
    result = []
    for key, (name, lat, lon) in TARGETS.items():
        item = {"key": key, "name": name, "lat": lat, "lon": lon, "waves": None, "tide": None, "temp": None}
        # golven: eerste meetpunt (op afstand) met recente Hm0
        for d, l in candidates(locs, per_loc, "Hm0", lat, lon)[:6]:
            try:
                hm0 = series(base, l, "Hm0", now - timedelta(hours=HOURS_BACK), now, proces)
            except Exception as ex:
                print(f"  {key} golven {l.get('Naam')}: {ex}", file=sys.stderr); continue
            if has_recent(hm0):
                w = {"hm0": hm0}
                for g, k in (("Tm02", "tm02"), ("Th0", "th0")):
                    try: w[k] = series(base, l, g, now - timedelta(hours=HOURS_BACK), now, proces)
                    except Exception as ex: print(f"  {key} {g} {l.get('Naam')}: {ex}", file=sys.stderr)
                item["waves"] = w; item["waves_name"] = l.get("Naam"); item["waves_km"] = round(d, 1)
                break
            print(f"  {key} golven: {l.get('Naam')} ({d:.0f} km) geen recente data")
        # getij: dichtstbijzijnde waterhoogte met recente meting; voorspelling astronomisch, anders verwachting
        for d, l in candidates(locs, per_loc, "WATHTE", lat, lon)[:6]:
            try:
                obs = series(base, l, "WATHTE", now - timedelta(hours=HOURS_BACK), now, proces)
            except Exception as ex:
                print(f"  {key} getij {l.get('Naam')}: {ex}", file=sys.stderr); continue
            if not has_recent(obs):
                print(f"  {key} getij: {l.get('Naam')} ({d:.0f} km) geen recente data"); continue
            fc = {"t": [], "v": []}
            for g, p in ((("WATHTE", "astronomisch"), ("WATHTE", "verwachting")) if base == NEW else (("WATHTBRKD", None),)):
                try:
                    fc = series(base, l, g, now - timedelta(minutes=10), now + timedelta(hours=HOURS_FWD), p)
                    if fc["t"]: break
                except Exception as ex:
                    print(f"  {key} {g} {p or ''} {l.get('Naam')}: {ex}", file=sys.stderr)
            last_obs = obs["t"][-1]
            allt = obs["t"] + [x for x in fc["t"] if x > last_obs]
            allv = obs["v"] + [fc["v"][i] for i, x in enumerate(fc["t"]) if x > last_obs]
            item["tide"] = {"obs": obs, "fc": fc, "extremes": extremes(allt, allv)}
            item["tide_name"] = l.get("Naam"); item["tide_km"] = round(d, 1)
            break
        # watertemperatuur
        for d, l in candidates(locs, per_loc, "T", lat, lon)[:6]:
            try:
                t = series(base, l, "T", now - timedelta(hours=HOURS_BACK), now, proces)
            except Exception as ex:
                print(f"  {key} T {l.get('Naam')}: {ex}", file=sys.stderr); continue
            if has_recent(t, hours=6):
                item["temp"] = t; item["temp_name"] = l.get("Naam"); item["temp_km"] = round(d, 1); break
        print(f"  {key}: golven={item.get('waves_name')} ({item.get('waves_km')} km), getij={item.get('tide_name')} ({item.get('tide_km')} km), temp={item.get('temp_name')} ({item.get('temp_km')} km)")
        result.append(item)
    if not any(i["waves"] or i["tide"] for i in result):
        raise RuntimeError("geen enkel meetpunt met data")
    return result


def latlon(l):
    """Nieuwe API geeft lat/lon (EPSG:4258); oude geeft UTM31 -> globaal omrekenen met pyproj als aanwezig."""
    try:
        for la, lo in (("Lat", "Lon"), ("Latitude", "Longitude"), ("lat", "lon"), ("Y", "X")):
            if l.get(la) is not None and l.get(lo) is not None and abs(float(l[la])) <= 90 and abs(float(l[lo])) <= 180:
                return float(l[la]), float(l[lo])
        if l.get("Geometrie") and isinstance(l["Geometrie"], dict) and l["Geometrie"].get("coordinates"):
            c = l["Geometrie"]["coordinates"]; return float(c[1]), float(c[0])
        cs = str(l.get("Coordinatenstelsel", ""))
        if cs in ("4258", "4326"):
            return float(l["Y"]), float(l["X"])
        from pyproj import Transformer
        lon, lat = Transformer.from_crs("EPSG:25831", "EPSG:4326", always_xy=True).transform(float(l["X"]), float(l["Y"]))
        return lat, lon
    except Exception:
        return None, None


def main():
    data, errors = None, []
    for base in (NEW, OLD):
        try:
            data = build(base)
            src = "Rijkswaterstaat WaterWebservices " + ("WADAR/ddapi20" if base == NEW else "DDL (oud)")
            break
        except Exception as ex:
            errors.append(f"{base}: {ex}")
            print("mislukt:", base, ex, file=sys.stderr)
    if data is None:
        if not OUT.exists():
            OUT.write_text(json.dumps({"error": " | ".join(errors), "locations": []}))
        print("sea overgeslagen:", errors, file=sys.stderr)
        return
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": src,
        "locations": data,
    }, separators=(",", ":"), ensure_ascii=False))
    print("sea.json geschreven:", ", ".join(f"{d['key']} golven={'ja' if d['waves'] else 'nee'} getij={'ja' if d['tide'] else 'nee'}" for d in data))


if __name__ == "__main__":
    main()
