"""
AIS-momentopname van de scheepvaart voor de Nederlandse kust -> site/ships.json

Bron: aisstream.io (gratis API-key, websocket). Luistert LISTEN_S seconden mee in
het gebied BBOX en bewaart per schip de laatste positie. Statische gegevens
(naam, type, bestemming, lengte) komen uit de ShipStaticData-berichten die in
die tijd voorbijkomen; ontbreken die, dan wordt de vorige ships.json gebruikt
om naam/type aan te vullen.

Env: AISSTREAM_KEY (secret); zonder key wordt het bestand met een melding geschreven.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent / "site" / "ships.json"
BBOX = [[51.30, 3.00], [53.70, 5.60]]   # [[lat_zuid, lon_west], [lat_noord, lon_oost]]: Nederlandse kust
LISTEN_S = 25


def load_previous():
    try:
        return {s["mmsi"]: s for s in json.loads(OUT.read_text()).get("ships", [])}
    except Exception:
        return {}


async def collect(key):
    import websockets
    ships, static = {}, {}
    sub = {"APIKey": key, "BoundingBoxes": [BBOX], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    async with websockets.connect("wss://stream.aisstream.io/v0/stream", ping_interval=None, max_size=None) as ws:
        await ws.send(json.dumps(sub))
        loop = asyncio.get_event_loop(); t_end = loop.time() + LISTEN_S
        while loop.time() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - loop.time()))
            except asyncio.TimeoutError:
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            meta = m.get("MetaData", {}); mmsi = meta.get("MMSI")
            if not mmsi:
                continue
            if m.get("MessageType") == "PositionReport":
                p = m["Message"]["PositionReport"]
                ships[mmsi] = {"mmsi": mmsi, "name": (meta.get("ShipName") or "").strip() or None, "lat": p.get("Latitude"), "lon": p.get("Longitude"),
                               "sog": p.get("Sog"), "cog": p.get("Cog") if p.get("Cog") not in (None, 360) else None,
                               "heading": p.get("TrueHeading") if p.get("TrueHeading") not in (None, 511) else None,
                               "nav": p.get("NavigationalStatus"), "ts": meta.get("time_utc")}
            elif m.get("MessageType") == "ShipStaticData":
                d = m["Message"]["ShipStaticData"]; dim = d.get("Dimension") or {}
                static[mmsi] = {"name": (d.get("Name") or "").strip() or None, "type": d.get("Type"), "dest": (d.get("Destination") or "").strip() or None,
                                "len": (dim.get("A") or 0) + (dim.get("B") or 0) or None}
    return ships, static


def main():
    key = (os.environ.get("AISSTREAM_KEY") or "").strip()
    if not key:
        OUT.write_text(json.dumps({"error": "AISSTREAM_KEY ontbreekt (gratis key via aisstream.io)", "ships": []}))
        print("ships overgeslagen: geen AISSTREAM_KEY"); return
    prev = load_previous()
    try:
        ships, static = asyncio.run(collect(key))
    except Exception as e:
        print("ships overgeslagen:", repr(e), file=sys.stderr)
        if not OUT.exists():
            OUT.write_text(json.dumps({"error": str(e), "ships": []}))
        return
    out = []
    for mmsi, s in ships.items():
        if s["lat"] is None or s["lon"] is None:
            continue
        st = static.get(mmsi) or {}; pv = prev.get(mmsi) or {}
        s["name"] = s["name"] or st.get("name") or pv.get("name")
        s["type"] = st.get("type", pv.get("type")); s["dest"] = st.get("dest", pv.get("dest")); s["len"] = st.get("len", pv.get("len"))
        out.append(s)
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "AIS via aisstream.io",
                               "bbox": [BBOX[0][0], BBOX[0][1], BBOX[1][0], BBOX[1][1]], "listen_s": LISTEN_S, "ships": out}, separators=(",", ":")))
    print(f"ships.json geschreven: {len(out)} schepen ({sum(1 for s in out if s.get('type') is not None)} met type)")


if __name__ == "__main__":
    main()
