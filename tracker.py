#!/usr/bin/env python3
"""PFP tracker — relevé de navigation du bord vers le site.

Lancé toutes les 30 min par un timer systemd (process oneshot, ~60 s de vie) :
  1. échantillonne Signal K en localhost (fallback : NMEA0183 en TCP direct) ;
  2. agrège les échantillons (médiane pour les vitesses, moyenne circulaire
     pour les angles — une médiane naïve est fausse autour de 0/360°) ;
  3. enfile le point dans une base SQLite locale ;
  4. envoie TOUTE la file par batchs vers l'Edge Function Supabase.

Coupure Starlink → les points s'accumulent dans la file et repartent en batch
au retour du lien. L'ingestion étant idempotente (upsert sur ts), un batch
renvoyé deux fois est inoffensif.
"""

import json
import math
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

# --- Configuration (surchargée par l'environnement, cf. config.example.env) ---
SUPABASE_FUNCTION_URL = os.environ.get("SUPABASE_FUNCTION_URL", "")
TRACKER_TOKEN = os.environ.get("TRACKER_TOKEN", "")
SIGNALK_URL = os.environ.get("SIGNALK_URL", "http://localhost:3000")
NMEA_TCP_HOST = os.environ.get("NMEA_TCP_HOST", "")  # fallback si Signal K est KO
NMEA_TCP_PORT = int(os.environ.get("NMEA_TCP_PORT", "10110"))
SAMPLES = int(os.environ.get("SAMPLES", "6"))
SAMPLE_INTERVAL_S = float(os.environ.get("SAMPLE_INTERVAL_S", "10"))
QUEUE_DB = os.environ.get("QUEUE_DB", os.path.expanduser("~/pfp-tracker/queue.db"))
QUEUE_MAX_ROWS = 5000
BATCH_SIZE = 100
HTTP_TIMEOUT_S = 30

MS_TO_KN = 1.94384


def log(msg: str) -> None:
    print(msg, flush=True)


# --- Lecture Signal K -----------------------------------------------------

def signalk_value(tree: dict, *path: str):
    """Extrait vessels/self/<path>/value du document Signal K, ou None."""
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node.get("value") if isinstance(node, dict) else None


def sample_signalk() -> dict | None:
    url = f"{SIGNALK_URL}/signalk/v1/api/vessels/self"
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            tree = json.load(res)
    except Exception as exc:  # noqa: BLE001 — réseau bateau : tout peut arriver
        log(f"signalk KO: {exc}")
        return None

    pos = signalk_value(tree, "navigation", "position")
    if not pos or pos.get("latitude") is None:
        return None

    def num(*path):
        value = signalk_value(tree, *path)
        return float(value) if isinstance(value, (int, float)) else None

    sog = num("navigation", "speedOverGround")
    cog = num("navigation", "courseOverGroundTrue")
    tws = num("environment", "wind", "speedTrue")
    twd = num("environment", "wind", "directionTrue")
    return {
        "lat": float(pos["latitude"]),
        "lon": float(pos["longitude"]),
        "sog_kn": sog * MS_TO_KN if sog is not None else None,
        "cog_deg": math.degrees(cog) % 360 if cog is not None else None,
        "tws_kn": tws * MS_TO_KN if tws is not None else None,
        "twd_deg": math.degrees(twd) % 360 if twd is not None else None,
    }


# --- Fallback : NMEA0183 en TCP direct (gateway type YDWG-02) --------------

def sample_nmea_tcp() -> dict | None:
    try:
        import pynmea2  # optionnel : uniquement pour le fallback
    except ImportError:
        log("pynmea2 absent — fallback NMEA indisponible (pip install pynmea2)")
        return None

    import socket

    sample: dict = {}
    deadline = time.monotonic() + 15
    try:
        with socket.create_connection((NMEA_TCP_HOST, NMEA_TCP_PORT), timeout=10) as sock:
            buf = b""
            while time.monotonic() < deadline and "lat" not in sample:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = pynmea2.parse(line.decode("ascii", "ignore").strip())
                    except Exception:  # noqa: BLE001 — trames corrompues fréquentes
                        continue
                    if msg.sentence_type == "RMC" and msg.latitude:
                        sample["lat"] = float(msg.latitude)
                        sample["lon"] = float(msg.longitude)
                        sample["sog_kn"] = float(msg.spd_over_grnd or 0)
                        if msg.true_course is not None:
                            sample["cog_deg"] = float(msg.true_course) % 360
                    elif msg.sentence_type == "MWD":
                        if getattr(msg, "wind_speed_knots", None):
                            sample["tws_kn"] = float(msg.wind_speed_knots)
                        if getattr(msg, "direction_true", None):
                            sample["twd_deg"] = float(msg.direction_true) % 360
    except OSError as exc:
        log(f"nmea tcp KO: {exc}")
        return None
    return sample if "lat" in sample else None


# --- Agrégation ------------------------------------------------------------

def circular_mean_deg(values: list[float]) -> float:
    x = sum(math.cos(math.radians(v)) for v in values)
    y = sum(math.sin(math.radians(v)) for v in values)
    return math.degrees(math.atan2(y, x)) % 360


def aggregate(samples: list[dict]) -> dict:
    def collect(key):
        return [s[key] for s in samples if s.get(key) is not None]

    point = {
        "ts": datetime.now(timezone.utc).isoformat(),
        # Dernière position telle quelle : c'est là où est le bateau.
        "lat": round(samples[-1]["lat"], 5),
        "lon": round(samples[-1]["lon"], 5),
        "source": "pi",
    }
    for key in ("sog_kn", "tws_kn"):
        values = collect(key)
        if values:
            point[key] = round(statistics.median(values), 1)
    for key in ("cog_deg", "twd_deg"):
        values = collect(key)
        if values:
            point[key] = round(circular_mean_deg(values), 1)
    return point


# --- File locale + envoi ----------------------------------------------------

def open_queue() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(QUEUE_DB), exist_ok=True)
    db = sqlite3.connect(QUEUE_DB)
    db.execute("create table if not exists queue (ts text primary key, payload text not null)")
    return db


def enqueue(db: sqlite3.Connection, point: dict) -> None:
    db.execute(
        "insert or ignore into queue (ts, payload) values (?, ?)",
        (point["ts"], json.dumps(point)),
    )
    # Purge des plus anciens si la file déborde (~100 jours de coupure à 30 min).
    db.execute(
        "delete from queue where ts not in (select ts from queue order by ts desc limit ?)",
        (QUEUE_MAX_ROWS,),
    )
    db.commit()


def send_batch(points: list[dict]) -> bool:
    req = urllib.request.Request(
        SUPABASE_FUNCTION_URL,
        data=json.dumps({"points": points}).encode(),
        headers={"content-type": "application/json", "x-tracker-token": TRACKER_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as res:
            return 200 <= res.status < 300
    except Exception as exc:  # noqa: BLE001
        log(f"envoi KO: {exc}")
        return False


def flush_queue(db: sqlite3.Connection) -> None:
    while True:
        rows = db.execute(
            "select ts, payload from queue order by ts asc limit ?", (BATCH_SIZE,)
        ).fetchall()
        if not rows:
            return
        points = [json.loads(payload) for _, payload in rows]
        if not send_batch(points):
            log(f"{len(rows)} point(s) en attente dans la file")
            return
        db.executemany("delete from queue where ts = ?", [(ts,) for ts, _ in rows])
        db.commit()
        log(f"{len(rows)} point(s) envoyé(s)")


def main() -> int:
    if not SUPABASE_FUNCTION_URL or not TRACKER_TOKEN:
        log("SUPABASE_FUNCTION_URL / TRACKER_TOKEN manquants (cf. config.example.env)")
        return 1

    samples = []
    for i in range(SAMPLES):
        sample = sample_signalk()
        if sample is None and NMEA_TCP_HOST:
            sample = sample_nmea_tcp()
        if sample:
            samples.append(sample)
        if i < SAMPLES - 1:
            time.sleep(SAMPLE_INTERVAL_S)

    db = open_queue()
    if samples:
        enqueue(db, aggregate(samples))
    else:
        log("aucun échantillon nav — on tente quand même de vider la file")

    flush_queue(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
