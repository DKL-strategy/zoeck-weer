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
WANTED = ["scheveningen", "ijmuiden", "europlatform", "euro platform"]   # substring in naam of code
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


def pick_locations(locs, per_loc):
    """Per gewenste plaats: het meetpunt met golfdata en het meetpunt met waterhoogte (kunnen verschillen)."""
    out = {}
    for lid, l in locs.items():
        name = (l.get("Naam") or "").lower(); code = (l.get("Code") or "").lower()
        key = next((w for w in WANTED if w in name or w in code), None)
        if not key:
            continue
        key = "europlatform" if key.startswith("euro") else key
        g = per_loc.get(lid, set())
        entry = out.setdefault(key, {"waves": None, "tide": None, "temp": None})
        if ("OW", "Hm0") in g and not entry["waves"]:
            entry["waves"] = l
        if ("OW", "WATHTE") in g and not entry["tide"]:
            entry["tide"] = l
        if ("OW", "T") in g and not entry["temp"]:
            entry["temp"] = l
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


def build(base):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    locs, per_loc = catalogus(base)
    picked = pick_locations(locs, per_loc)
    print(f"{base}: {len(locs)} locaties in catalogus; gevonden: " + ", ".join(f"{k} (golven={bool(v['waves'])}, getij={bool(v['tide'])})" for k, v in picked.items()))
    sample = next((v["waves"] or v["tide"] for v in picked.values() if v["waves"] or v["tide"]), None)
    if sample:
        print("  voorbeeld locatie-record:", {k: sample[k] for k in list(sample)[:12]})
    if not picked:
        raise RuntimeError("geen gewenste locaties gevonden")
    result = []
    for key, e in picked.items():
        item = {"key": key, "name": key.capitalize(), "waves": None, "tide": None, "temp": None}
        ref = e["waves"] or e["tide"]
        item["lat"], item["lon"] = latlon(ref)
        if e["waves"]:
            item["waves_name"] = e["waves"].get("Naam")
            w = {}
            for g, k in (("Hm0", "hm0"), ("Tm02", "tm02"), ("Th0", "th0")):
                try:
                    w[k] = series(base, e["waves"], g, now - timedelta(hours=HOURS_BACK), now, "meting" if base == NEW else None)
                except Exception as ex:
                    print(f"  {key} {g}: {ex}", file=sys.stderr)
            if w.get("hm0", {}).get("t"):
                item["waves"] = w
        if e["tide"]:
            item["tide_name"] = e["tide"].get("Naam")
            try:
                obs = series(base, e["tide"], "WATHTE", now - timedelta(hours=HOURS_BACK), now, "meting" if base == NEW else None)
            except Exception as ex:
                obs = {"t": [], "v": []}; print(f"  {key} WATHTE meting: {ex}", file=sys.stderr)
            fc = {"t": [], "v": []}
            # nieuw: astronomisch getij (altijd 48 u vooruit), anders weersafhankelijke verwachting; oud: WATHTBRKD
            for g, p in ((("WATHTE", "astronomisch"), ("WATHTE", "verwachting")) if base == NEW else (("WATHTBRKD", None),)):
                try:
                    fc = series(base, e["tide"], g, now - timedelta(minutes=10), now + timedelta(hours=HOURS_FWD), p)
                    if fc["t"]:
                        break
                except Exception as ex:
                    print(f"  {key} {g} {p or ''}: {ex}", file=sys.stderr)
            allt = obs["t"] + [x for x in fc["t"] if x > (obs["t"][-1] if obs["t"] else "")]
            allv = obs["v"] + [fc["v"][i] for i, x in enumerate(fc["t"]) if x > (obs["t"][-1] if obs["t"] else "")]
            if allt:
                item["tide"] = {"obs": obs, "fc": fc, "extremes": extremes(allt, allv)}
        if e["temp"]:
            try:
                item["temp"] = series(base, e["temp"], "T", now - timedelta(hours=HOURS_BACK), now, "meting" if base == NEW else None)
            except Exception as ex:
                print(f"  {key} T: {ex}", file=sys.stderr)
        result.append(item)
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
