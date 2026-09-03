"""
Haalt de nieuwste KNMI neerslag-nowcast op (dataset radar_forecast 2.0: radar
+ pySTEPS, 25 stappen van +0 tot +120 min, 1x1 km) en leest voor elk station
uit site/data.json de waarde in het bijbehorende rastervak. Schrijft
site/radar.json met per station 25 waarden in mm/uur.

Env:  KNMI_API_KEY   (Open Data API-key; dezelfde key als voor de EDR API werkt
                      doorgaans niet — vraag in het KNMI-portaal een key aan
                      voor de Open Data API en zet die als KNMI_OPEN_DATA_KEY,
                      anders wordt KNMI_API_KEY geprobeerd)
"""
import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import numpy as np
import requests
from pyproj import Proj

DATASET, VERSION = "radar_forecast", "2.0"
API = f"https://api.dataplatform.knmi.nl/open-data/v1/datasets/{DATASET}/versions/{VERSION}/files"
SITE = Path(__file__).parent / "site"
DATA, OUT = SITE / "data.json", SITE / "radar.json"
GRID_OUT = SITE / "radar-grid.json"
# Raster voor de kaart: regelmatig lat/lon-rooster over Nederland (~2,5 km)
GRID = {"lat0": 50.70, "lat1": 53.60, "lon0": 3.20, "lon1": 7.30, "dlat": 0.0225, "dlon": 0.036}


def key():
    k = (os.environ.get("KNMI_OPEN_DATA_KEY") or os.environ.get("KNMI_API_KEY") or "").strip()
    if not k:
        sys.exit("KNMI_OPEN_DATA_KEY of KNMI_API_KEY ontbreekt")
    return k


def latest_file():
    h = {"Authorization": key()}
    r = requests.get(API, params={"maxKeys": 1, "orderBy": "created", "sorting": "desc"}, headers=h, timeout=30)
    r.raise_for_status()
    files = r.json().get("files", [])
    if not files:
        raise RuntimeError("geen bestanden in dataset")
    name = files[0]["filename"]
    r = requests.get(f"{API}/{name}/url", headers=h, timeout=30)
    r.raise_for_status()
    url = r.json()["temporaryDownloadUrl"]
    tmp = Path(tempfile.gettempdir()) / name
    with requests.get(url, stream=True, timeout=120) as d:
        d.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in d.iter_content(1 << 16):
                f.write(chunk)
    return name, tmp


def attr(obj, name, default=None):
    v = obj.attrs.get(name, default)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    if isinstance(v, np.ndarray) and v.dtype.kind == "S":
        v = v[0].decode("utf-8", "ignore")
    if isinstance(v, np.ndarray) and v.size == 1:
        v = v.item()
    return v


def parse_time(s):
    """KNMI-notatie '10-SEP-2026;12:35:00.000' -> datetime (UTC)."""
    try:
        return datetime.strptime(s.split(".")[0], "%d-%b-%Y;%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def grid_mapper(f):
    """Geeft functie (lat, lon) -> (row, col) op basis van projectie en hoekpunten."""
    geo = f["geographic"]
    proj4 = attr(f["geographic/map_projection"], "projection_proj4_params")
    proj = Proj(proj4)
    corners = np.array(attr(geo, "geo_product_corners"), dtype=float).reshape(4, 2)  # lon, lat
    try:
        ncols = int(np.array(attr(geo, "geo_number_columns")).ravel()[0])
        nrows = int(np.array(attr(geo, "geo_number_rows")).ravel()[0])
    except Exception:  # niet in attributen: afleiden uit het eerste beeld
        nrows, ncols = f["image1/image_data"].shape
    xs, ys = proj(corners[:, 0], corners[:, 1])
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()

    def to_rc(lat, lon):
        x, y = proj(lon, lat)
        col = (x - x0) / (x1 - x0) * ncols
        row = (y1 - y) / (y1 - y0) * nrows
        return int(row), int(col)
    return to_rc, nrows, ncols


def read_image(f, k):
    g = f[f"image{k}"]
    data = g["image_data"][()]
    cal = g["calibration"] if "calibration" in g else None
    a, b = 1.0, 0.0
    missing, out = None, None
    if cal is not None:
        m = re.search(r"GEO\s*=\s*([-\d.eE+]+)\s*\*\s*PV\s*\+\s*([-\d.eE+]+)", str(attr(cal, "calibration_formulas", "")))
        if m:
            a, b = float(m.group(1)), float(m.group(2))
        missing = attr(cal, "calibration_missing_data")
        out = attr(cal, "calibration_out_of_image")
    valid = parse_time(str(attr(g, "image_datetime_valid", "")))
    return data, a, b, missing, out, valid


def main():
    stations = json.loads(DATA.read_text())["stations"]
    name, path = latest_file()
    print("bestand:", name)
    m = re.search(r"(\d{12})", name)
    base = datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc) if m else datetime.now(timezone.utc)

    with h5py.File(path, "r") as f:
        to_rc, nrows, ncols = grid_mapper(f)
        n_img = len([k for k in f.keys() if k.startswith("image")])
        times, values = [], {s["id"]: [] for s in stations}
        rc = {s["id"]: to_rc(s["lat"], s["lon"]) if s.get("lat") is not None else None for s in stations}
        # rooster voor de kaart: rij/kolom van elk roostercelmidden in het KNMI-beeld
        lats = np.arange(GRID["lat1"], GRID["lat0"], -GRID["dlat"])   # noord -> zuid (beeldvolgorde)
        lons = np.arange(GRID["lon0"], GRID["lon1"], GRID["dlon"])
        LON, LAT = np.meshgrid(lons, lats)
        geo = f["geographic"]
        proj = Proj(attr(f["geographic/map_projection"], "projection_proj4_params"))
        corners = np.array(attr(geo, "geo_product_corners"), dtype=float).reshape(4, 2)
        cx, cy = proj(corners[:, 0], corners[:, 1])
        gx, gy = proj(LON, LAT)
        frames = []

        for k in range(1, n_img + 1):
            data, a, b, missing, out, valid = read_image(f, k)
            times.append((valid or base + timedelta(minutes=5 * (k - 1))).strftime("%Y-%m-%dT%H:%M:%SZ"))
            # frame: mm/uur * 10 als byte (0-255), buiten beeld/missing = 0
            gcol = ((gx - cx.min()) / (cx.max() - cx.min()) * data.shape[1]).astype(int)
            grow = ((cy.max() - gy) / (cy.max() - cy.min()) * data.shape[0]).astype(int)
            inside = (grow >= 0) & (grow < data.shape[0]) & (gcol >= 0) & (gcol < data.shape[1])
            pv = np.zeros(gx.shape, dtype=np.float64)
            pv[inside] = data[grow[inside], gcol[inside]].astype(np.float64)
            bad = ~inside
            if missing is not None: bad |= (pv == missing)
            if out is not None: bad |= (pv == out)
            mmh = np.clip((a * pv + b) * 12.0, 0, 25.5)
            mmh[bad] = 0
            frames.append(base64.b64encode(np.round(mmh * 10).astype(np.uint8).tobytes()).decode("ascii"))
            for sid, pos in rc.items():
                if pos is None or not (0 <= pos[0] < data.shape[0] and 0 <= pos[1] < data.shape[1]):
                    values[sid].append(None)
                    continue
                pv = data[pos[0], pos[1]]
                if (missing is not None and pv == missing) or (out is not None and pv == out):
                    values[sid].append(None)
                else:
                    mm5 = a * float(pv) + b          # neerslagsom per 5 minuten (mm)
                    values[sid].append(round(max(0.0, mm5) * 12, 2))  # -> mm/uur

    GRID_OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "KNMI radar_forecast 2.0 (radar + pySTEPS nowcast), hersampled naar lat/lon-rooster",
        "lat0": float(lats[-1]), "lat1": float(lats[0]), "lon0": float(lons[0]), "lon1": float(lons[-1]),
        "nlat": int(len(lats)), "nlon": int(len(lons)), "dlat": GRID["dlat"], "dlon": GRID["dlon"],
        "scale": 10, "unit": "mm/h", "times": times, "frames": frames,
    }, separators=(",", ":")))
    print(f"radar-grid.json geschreven: {len(lats)}x{len(lons)} cellen, {len(frames)} frames")

    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "KNMI radar_forecast 2.0 (radar + pySTEPS nowcast)",
        "file": name,
        "times": times,
        "stations": values,
    }, separators=(",", ":")))
    nonempty = sum(1 for v in values.values() if any(x is not None for x in v))
    print(f"radar.json geschreven: {len(times)} stappen, {nonempty}/{len(values)} stations met waarden")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # nooit de hele run laten falen op de radar
        print("radar overgeslagen:", repr(e), file=sys.stderr)
        if not OUT.exists():
            GRID_OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "KNMI radar_forecast 2.0 (radar + pySTEPS nowcast), hersampled naar lat/lon-rooster",
        "lat0": float(lats[-1]), "lat1": float(lats[0]), "lon0": float(lons[0]), "lon1": float(lons[-1]),
        "nlat": int(len(lats)), "nlon": int(len(lons)), "dlat": GRID["dlat"], "dlon": GRID["dlon"],
        "scale": 10, "unit": "mm/h", "times": times, "frames": frames,
    }, separators=(",", ":")))
    print(f"radar-grid.json geschreven: {len(lats)}x{len(lons)} cellen, {len(frames)} frames")

    OUT.write_text(json.dumps({"error": str(e), "times": [], "stations": {}}))
