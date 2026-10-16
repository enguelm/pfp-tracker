#!/usr/bin/env python3
"""PFP tracker — relevé de navigation du bord vers le site.

Lancé toutes les 30 min par un timer systemd (process oneshot, ~70 s de vie) :
  1. écoute le flux NMEA 0183 de la passerelle Yacht Devices (YDEN-03,
     TCP 1456, réglages usine) pendant SAMPLE_WINDOW_S secondes. La passerelle
     calcule elle-même le vent vrai (phrase MWD) et fournit l'heure GPS (RMC) ;
     à défaut de passerelle joignable, interroge Signal K si SIGNALK_URL
     est renseigné ;
  2. agrège (médiane des vitesses, moyenne circulaire des angles — une médiane
     naïve est fausse autour de 0/360°) et horodate avec l'heure GPS, jamais
     avec l'horloge du Pi si elle n'est pas synchronisée ;
  3. enfile le point dans une base SQLite locale ;
  4. envoie la file par lots vers l'Edge Function Supabase, si configurée.

Coupure Starlink → les points s'accumulent dans la file et repartent au retour
du lien. L'ingestion est idempotente (upsert sur ts). Un lot refusé par le
serveur (400) est bissecté pour isoler et jeter le seul point fautif : la file
ne peut pas rester bloquée.

Zéro dépendance hors bibliothèque standard (Python ≥ 3.9).

Usage : tracker.py [--dry-run] [--flush-only] [--window SECONDES]
"""

import argparse
import json
import math
import os
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# --- Configuration (surchargée par l'environnement, cf. config.example.env) ---
# Une ou plusieurs adresses séparées par des virgules, essayées dans l'ordre
# (ex. « 192.168.10.20,yden.local » : l'IP fixe, puis mDNS si l'IP a changé).
NMEA_TCP_HOSTS = [h.strip() for h in os.environ.get("NMEA_TCP_HOST", "yden.local").split(",") if h.strip()]
NMEA_TCP_PORT = int(os.environ.get("NMEA_TCP_PORT", "1456"))
SIGNALK_URL = os.environ.get("SIGNALK_URL", "").rstrip("/")  # vide = désactivé
SAMPLE_WINDOW_S = float(os.environ.get("SAMPLE_WINDOW_S", "60"))
MAX_AGE_S = float(os.environ.get("MAX_AGE_S", "60"))  # fraîcheur exigée (Signal K)
SUPABASE_FUNCTION_URL = os.environ.get("SUPABASE_FUNCTION_URL", "")
TRACKER_TOKEN = os.environ.get("TRACKER_TOKEN", "")
QUEUE_DB = os.path.expanduser(os.environ.get("QUEUE_DB", "~/pfp-tracker/queue.db"))
QUEUE_MAX_ROWS = 5000  # ~100 jours de coupure à un point / 30 min
BATCH_SIZE = 100
HTTP_TIMEOUT_S = 30
MIN_COG_SOG_KN = 0.5  # en dessous, le cap fond est du bruit
# Sans cap compas (pilote éteint : l'EV-1 ne publie plus rien), la passerelle
# oriente le vent vrai avec le cap fond : à l'arrêt (mouillage, ponton) la
# direction obtenue est aléatoire et n'est pas publiée.
MIN_TWD_SOG_KN = 2.0

MS_TO_KN = 1.94384
KMH_TO_KN = 0.539957


def log(msg: str) -> None:
    print(msg, flush=True)


# --- Accumulateur d'échantillons ------------------------------------------

class Samples:
    """Listes de valeurs brutes collectées pendant la fenêtre d'écoute."""

    def __init__(self) -> None:
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.fix_time: Optional[datetime] = None  # heure GPS du dernier fix
        self.sog: List[float] = []
        self.cog: List[float] = []
        self.tws: List[float] = []
        self.twd: List[float] = []
        self.twa: List[float] = []  # angle vent vrai / étrave, si pas de TWD
        self.mwv_tws: List[float] = []  # vitesse vent vrai via MWV, secours de MWD
        self.heading: List[float] = []  # cap vrai : HDT ou Signal K
        self.heading_hdg: List[float] = []  # cap vrai via HDG + déclinaison, secours de HDT
        self.compass = False  # cap compas reçu (HDT/HDG/HDM), même sans déclinaison
        self.sentences = 0  # phrases NMEA valides reçues (diagnostic)
        self.rmc_seen = False  # RMC prioritaire sur GLL/VTG

    def true_heading(self) -> List[float]:
        return self.heading or self.heading_hdg

    def has_fix(self) -> bool:
        return self.lat is not None and self.lon is not None

    def set_fix(self, lat: float, lon: float, when: Optional[datetime]) -> None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return
        self.lat, self.lon = lat, lon
        if when is not None:
            self.fix_time = when

    def add_motion(self, sog_kn: Optional[float], cog_deg: Optional[float]) -> None:
        if sog_kn is not None and 0 <= sog_kn < 40:
            self.sog.append(sog_kn)
            if cog_deg is not None and sog_kn >= MIN_COG_SOG_KN:
                self.cog.append(cog_deg % 360)

    def add_wind(self, tws_kn: Optional[float], twd_deg: Optional[float]) -> None:
        if tws_kn is not None and 0 <= tws_kn < 130:
            self.tws.append(tws_kn)
        if twd_deg is not None:
            self.twd.append(twd_deg % 360)

    def add_heading(self, heading_deg: Optional[float]) -> None:
        if heading_deg is not None:
            self.heading.append(heading_deg % 360)


# --- NMEA 0183 (passerelle Yacht Devices) ---------------------------------

def nmea_fields(line: str) -> Optional[List[str]]:
    """Découpe une phrase NMEA en champs après contrôle du checksum."""
    line = line.strip()
    if not line.startswith(("$", "!")) or "*" not in line:
        return None
    body, _, checksum = line[1:].partition("*")
    try:
        expected = int(checksum[:2], 16)
    except ValueError:
        return None
    actual = 0
    for ch in body:
        actual ^= ord(ch)
    if actual != expected:
        return None
    return body.split(",")


def nmea_float(value: str) -> Optional[float]:
    try:
        number = float(value) if value else None
    except ValueError:
        return None
    # float() accepte « nan » et « inf » : une moyenne d'angles avec un NaN
    # vaudrait 0,0° sans erreur, d'où le filtre.
    return number if number is not None and math.isfinite(number) else None


def nmea_coord(value: str, hemisphere: str) -> Optional[float]:
    """'4916.45','N' → 49.274167 ; 'ddmm.mmmm' / 'dddmm.mmmm'."""
    if not value or hemisphere not in ("N", "S", "E", "W"):
        return None
    dot = value.find(".")
    if dot == -1:
        dot = len(value)
    try:
        degrees = int(value[: dot - 2])
        minutes = float(value[dot - 2 :])
    except ValueError:
        return None
    coord = degrees + minutes / 60
    return -coord if hemisphere in ("S", "W") else coord


def nmea_datetime(date_ddmmyy: str, time_hhmmss: str) -> Optional[datetime]:
    try:
        day, month, year = int(date_ddmmyy[0:2]), int(date_ddmmyy[2:4]), 2000 + int(date_ddmmyy[4:6])
        hour, minute, second = int(time_hhmmss[0:2]), int(time_hhmmss[2:4]), float(time_hhmmss[4:])
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc) + timedelta(seconds=second)
    except (ValueError, IndexError):
        return None


def feed_nmea(samples: Samples, fields: List[str]) -> None:
    """Alimente l'accumulateur avec une phrase découpée (talker ignoré)."""
    kind = fields[0][-3:]
    f = fields + [""] * 12  # évite les IndexError sur les phrases courtes
    samples.sentences += 1

    if kind == "RMC" and f[2] == "A":  # A = fix valide, V = invalide
        lat, lon = nmea_coord(f[3], f[4]), nmea_coord(f[5], f[6])
        if lat is not None and lon is not None:
            samples.rmc_seen = True
            samples.set_fix(lat, lon, nmea_datetime(f[9], f[1]))
            samples.add_motion(nmea_float(f[7]), nmea_float(f[8]))

    # Secours si le GPS du bus n'émet pas la PGN 129029 : la passerelle ne
    # produit alors pas de RMC, seulement GLL (position, sans date) et VTG.
    elif kind == "GLL" and f[6] == "A" and not samples.rmc_seen:
        lat, lon = nmea_coord(f[1], f[2]), nmea_coord(f[3], f[4])
        if lat is not None and lon is not None:
            samples.set_fix(lat, lon, None)

    elif kind == "VTG" and f[9] != "N" and not samples.rmc_seen:
        samples.add_motion(nmea_float(f[5]), nmea_float(f[1]))

    elif kind == "MWD":  # vent vrai calculé par la passerelle
        tws = nmea_float(f[5])  # nœuds
        if tws is None and nmea_float(f[7]) is not None:
            tws = nmea_float(f[7]) * MS_TO_KN  # m/s
        samples.add_wind(tws, nmea_float(f[1]))

    elif kind == "MWV" and f[2] == "T" and f[5] == "A":  # vent vrai relatif à l'étrave
        speed = nmea_float(f[3])
        if speed is not None:
            speed *= {"N": 1.0, "M": MS_TO_KN, "K": KMH_TO_KN}.get(f[4], 1.0)
        angle = nmea_float(f[1])
        if angle is not None:
            samples.twa.append(angle % 360)
        if speed is not None and 0 <= speed < 130:
            samples.mwv_tws.append(speed)  # MWD reste prioritaire (cf. aggregate)

    elif kind == "HDT":
        heading = nmea_float(f[1])
        samples.compass |= heading is not None
        samples.add_heading(heading)

    elif kind == "HDG":  # cap magnétique + déclinaison → cap vrai
        heading = nmea_float(f[1])
        variation = nmea_float(f[4])
        samples.compass |= heading is not None
        if heading is not None and variation is not None:
            samples.heading_hdg.append((heading + (variation if f[5] == "E" else -variation)) % 360)

    elif kind == "HDM":  # cap magnétique seul : prouve la présence du compas
        samples.compass |= nmea_float(f[1]) is not None


def _read_stream(sock: socket.socket, samples: Samples, deadline: float) -> None:
    """Lit le flux jusqu'à l'échéance ou la coupure, sans jamais lever."""
    sock.settimeout(5)
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            log(f"nmea: connexion coupée en cours d'écoute ({exc})")
            return
        if not chunk:
            log("nmea: connexion fermée par la passerelle")
            return
        *lines, buf = (buf + chunk).split(b"\n")
        buf = buf[-4096:]  # flux sans saut de ligne : pas de croissance sans fin
        for line in lines:
            fields = nmea_fields(line.decode("ascii", "ignore"))
            if not fields:
                continue
            try:
                feed_nmea(samples, fields)
            except Exception as exc:  # noqa: BLE001 — une phrase inattendue ne doit pas perdre la fenêtre
                log(f"nmea: phrase ignorée ({exc!r}) : {line[:80]!r}")


def sample_nmea(host: str, port: int, window_s: float) -> Optional[Samples]:
    """Écoute la passerelle pendant window_s secondes. None si injoignable.

    Une coupure en cours d'écoute garde ce qui a déjà été reçu et tente de se
    reconnecter jusqu'à l'échéance."""
    samples = Samples()
    deadline = time.monotonic() + window_s
    connected = False
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            if not connected:
                log(f"nmea: passerelle {host}:{port} injoignable ({exc})")
                return None
            log(f"nmea: reconnexion impossible ({exc})")
            break
        connected = True
        with sock:
            _read_stream(sock, samples, deadline)
        if time.monotonic() < deadline:
            time.sleep(2)
    log(f"nmea: {samples.sentences} phrases reçues en {window_s:.0f} s ({host})")
    return samples


def sample_gateway(hosts: List[str], port: int, window_s: float) -> Optional[Samples]:
    """Première adresse joignable de la liste. None si aucune ne répond."""
    for host in hosts:
        samples = sample_nmea(host, port, window_s)
        if samples is not None:
            return samples
    return None


# --- Signal K (optionnel) ---------------------------------------------------

def _leaf(tree: dict, *path: str):
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def signalk_value(tree: dict, *path: str):
    """Valeur Signal K si elle a moins de MAX_AGE_S secondes, sinon None."""
    leaf = _leaf(tree, *path)
    if not leaf or leaf.get("value") is None:
        return None
    stamp = _parse_iso(leaf.get("timestamp", ""))
    if stamp is None or (datetime.now(timezone.utc) - stamp).total_seconds() > MAX_AGE_S:
        return None
    return leaf["value"]


def ground_wind(awa_rad: float, aws_ms: float, heading_rad: float, sog_ms: float, cog_rad: float):
    """Vent vrai (fond) depuis le vent apparent et le déplacement du bateau.

    Retourne (tws_kn, twd_deg). Vecteurs (est, nord) ; le vent apparent souffle
    *vers* l'opposé de sa provenance (cap + AWA)."""
    from_bearing = heading_rad + awa_rad
    apparent_e, apparent_n = -aws_ms * math.sin(from_bearing), -aws_ms * math.cos(from_bearing)
    boat_e, boat_n = sog_ms * math.sin(cog_rad), sog_ms * math.cos(cog_rad)
    true_e, true_n = apparent_e + boat_e, apparent_n + boat_n
    tws = math.hypot(true_e, true_n)
    twd = math.degrees(math.atan2(-true_e, -true_n)) % 360 if tws > 0.05 else None
    return tws * MS_TO_KN, twd


def feed_signalk(samples: Samples, tree: dict) -> None:
    pos = signalk_value(tree, "navigation", "position")
    if not pos or pos.get("latitude") is None:
        return
    samples.sentences += 1
    samples.set_fix(float(pos["latitude"]), float(pos["longitude"]),
                    _parse_iso(signalk_value(tree, "navigation", "datetime") or ""))

    def num(*path):
        value = signalk_value(tree, *path)
        ok = isinstance(value, (int, float)) and math.isfinite(value)
        return float(value) if ok else None

    sog = num("navigation", "speedOverGround")
    variation = num("navigation", "magneticVariation") or 0.0
    cog = num("navigation", "courseOverGroundTrue")
    if cog is None and num("navigation", "courseOverGroundMagnetic") is not None:
        cog = num("navigation", "courseOverGroundMagnetic") + variation
    heading = num("navigation", "headingTrue")
    if heading is None and num("navigation", "headingMagnetic") is not None:
        heading = num("navigation", "headingMagnetic") + variation

    samples.add_motion(sog * MS_TO_KN if sog is not None else None,
                       math.degrees(cog) if cog is not None else None)
    samples.add_heading(math.degrees(heading) if heading is not None else None)

    twd = num("environment", "wind", "directionTrue")
    tws = num("environment", "wind", "speedTrue")
    if tws is None:
        tws = num("environment", "wind", "speedOverGround")
    if twd is None or tws is None:
        awa, aws = num("environment", "wind", "angleApparent"), num("environment", "wind", "speedApparent")
        reference = heading if heading is not None else cog  # sans compas : le cap fond
        if None not in (awa, aws, reference, sog, cog):
            tws_kn, twd_deg = ground_wind(awa, aws, reference, sog, cog)
            samples.add_wind(tws_kn, twd_deg)
            return
    samples.add_wind(tws * MS_TO_KN if tws is not None else None,
                     math.degrees(twd) if twd is not None else None)


def sample_signalk(url: str, window_s: float) -> Optional[Samples]:
    """Interroge Signal K toutes les 10 s pendant window_s. None si injoignable."""
    samples = Samples()
    polls = max(1, int(window_s // 10))
    reachable = False
    for i in range(polls):
        try:
            with urllib.request.urlopen(f"{url}/signalk/v1/api/vessels/self", timeout=5) as res:
                feed_signalk(samples, json.load(res))
            reachable = True
        except Exception as exc:  # noqa: BLE001 — réseau bateau : tout peut arriver
            log(f"signalk: {exc}")
        if i < polls - 1:
            time.sleep(10)
    return samples if reachable else None


# --- Horodatage et agrégation ---------------------------------------------

def system_clock_is_trusted() -> bool:
    """Vrai si l'horloge du Pi est synchronisée par NTP (le Pi n'a pas de RTC)."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "yes"
    except Exception:  # noqa: BLE001
        return False


def circular_mean_deg(values: List[float]) -> float:
    x = sum(math.cos(math.radians(v)) for v in values)
    y = sum(math.sin(math.radians(v)) for v in values)
    degrees = math.degrees(math.atan2(y, x)) % 360
    return degrees if degrees < 360 else 0.0  # -1e-17 % 360 vaut 360.0 en flottant


def round_angle(value: float) -> float:
    # Arrondir APRÈS le modulo peut donner 360.0 (ex. 359.96) : on re-normalise.
    return round(value, 1) % 360


def aggregate(samples: Optional[Samples]) -> Optional[dict]:
    if samples is None or not samples.has_fix():
        return None
    when = samples.fix_time
    if when is None:
        if not system_clock_is_trusted():
            log("pas d'heure GPS et horloge Pi non synchronisée : point ignoré")
            return None
        when = datetime.now(timezone.utc)

    point = {
        "ts": when.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "lat": round(samples.lat, 5),
        "lon": round(samples.lon, 5),
        "source": "pi",
    }
    if samples.sog:
        point["sog_kn"] = round(statistics.median(samples.sog), 1)
    if samples.cog:
        point["cog_deg"] = round_angle(circular_mean_deg(samples.cog))
    heading = samples.true_heading()
    if heading:
        point["heading_deg"] = round_angle(circular_mean_deg(heading))

    tws = samples.tws or samples.mwv_tws
    if tws:
        point["tws_kn"] = round(statistics.median(tws), 1)
    if samples.twd and (heading or samples.compass or point.get("sog_kn", 0) >= MIN_TWD_SOG_KN):
        point["twd_deg"] = round_angle(circular_mean_deg(samples.twd))
    elif samples.twa and heading:  # MWV vrai + cap → direction
        point["twd_deg"] = round_angle(circular_mean_deg(heading) + circular_mean_deg(samples.twa))
    return point


# --- File locale ------------------------------------------------------------

def _connect_queue(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    try:
        db.execute("create table if not exists queue (ts text primary key, payload text not null)")
        if db.execute("pragma quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("quick_check en échec")
    except sqlite3.DatabaseError:
        db.close()
        raise
    return db


def open_queue(path: str = QUEUE_DB) -> sqlite3.Connection:
    """Ouvre la file ; une base corrompue (coupure de courant, carte SD) est
    mise de côté et remplacée, sinon chaque passage planterait à vie."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        return _connect_queue(path)
    except sqlite3.DatabaseError as exc:
        broken = f"{path}.corrompu-{int(time.time())}"
        log(f"file locale illisible ({exc}) : mise de côté dans {broken}, nouvelle file")
        os.replace(path, broken)
        return _connect_queue(path)


def enqueue(db: sqlite3.Connection, point: dict) -> None:
    db.execute("insert or ignore into queue (ts, payload) values (?, ?)",
               (point["ts"], json.dumps(point)))
    db.execute("delete from queue where ts not in (select ts from queue order by ts desc limit ?)",
               (QUEUE_MAX_ROWS,))
    db.commit()


def queue_size(db: sqlite3.Connection) -> int:
    return db.execute("select count(*) from queue").fetchone()[0]


# --- Envoi ------------------------------------------------------------------

def send_batch(points: List[dict]) -> str:
    """'ok' | 'rejected' (400/422 : point invalide) | 'auth' (401/403) | 'retry' (tout le reste).

    Seul un refus explicite du contenu peut faire jeter un point : un 404
    (fonction pas encore déployée, URL fausse), un 429 ou un 5xx garde la file."""
    req = urllib.request.Request(
        SUPABASE_FUNCTION_URL,
        data=json.dumps({"points": points}).encode(),
        headers={"content-type": "application/json", "x-tracker-token": TRACKER_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as res:
            return "ok" if 200 <= res.status < 300 else "retry"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "auth"
        if exc.code in (400, 422):
            log(f"envoi refusé HTTP {exc.code}: {exc.read(200)!r}")
            return "rejected"
        log(f"envoi KO HTTP {exc.code} (file conservée) : {exc.read(200)!r}")
        return "retry"
    except Exception as exc:  # noqa: BLE001
        log(f"envoi KO: {exc}")
        return "retry"


def deliver(db: sqlite3.Connection, rows: list) -> str:
    """Envoie un lot ; sur refus, bissecte pour isoler et jeter le point fautif."""
    result = send_batch([json.loads(payload) for _, payload in rows])
    if result == "ok":
        db.executemany("delete from queue where ts = ?", [(ts,) for ts, _ in rows])
        db.commit()
        log(f"{len(rows)} point(s) envoyé(s)")
        return "ok"
    if result == "rejected":
        if len(rows) == 1:
            log(f"point {rows[0][0]} rejeté par le serveur, retiré de la file : {rows[0][1]}")
            db.execute("delete from queue where ts = ?", (rows[0][0],))
            db.commit()
            return "ok"
        half = len(rows) // 2
        first = deliver(db, rows[:half])
        return first if first != "ok" else deliver(db, rows[half:])
    if result == "auth":
        log("token refusé (401/403) : vérifier TRACKER_TOKEN dans config.env")
    return result


def flush_queue(db: sqlite3.Connection) -> None:
    pending = queue_size(db)
    if not SUPABASE_FUNCTION_URL or not TRACKER_TOKEN:
        log(f"envoi non configuré (SUPABASE_FUNCTION_URL / TRACKER_TOKEN) : {pending} point(s) conservé(s) dans la file")
        return
    while True:
        rows = db.execute("select ts, payload from queue order by ts asc limit ?", (BATCH_SIZE,)).fetchall()
        if not rows:
            return
        if deliver(db, rows) != "ok":
            log(f"{queue_size(db)} point(s) en attente dans la file")
            return


# --- Programme principal ----------------------------------------------------

def collect_point(window_s: float) -> Optional[dict]:
    samples = sample_gateway(NMEA_TCP_HOSTS, NMEA_TCP_PORT, window_s)
    if samples is None and SIGNALK_URL:
        samples = sample_signalk(SIGNALK_URL, window_s)
    point = aggregate(samples)
    if point:
        log("point: " + json.dumps(point, ensure_ascii=False))
    elif samples is None:
        log("aucune source de données joignable — pas de point ce passage")
    elif not samples.has_fix():
        log("aucune position GPS reçue (instruments éteints ?) — pas de point")
    return point


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PFP tracker — relevé nav → Supabase")
    parser.add_argument("--dry-run", action="store_true", help="échantillonne et affiche le point, sans file ni envoi")
    parser.add_argument("--flush-only", action="store_true", help="n'échantillonne pas, vide seulement la file")
    parser.add_argument("--window", type=float, default=SAMPLE_WINDOW_S, help="durée d'écoute en secondes")
    args = parser.parse_args(argv)

    point = None
    if not args.flush_only:
        try:
            point = collect_point(args.window)
        except Exception as exc:  # noqa: BLE001 — un relevé raté ne doit pas bloquer l'envoi de la file
            log(f"relevé KO, pas de point ce passage : {exc!r}")

    if args.dry_run:
        return 0 if point else 1

    db = open_queue(QUEUE_DB)
    if point:
        enqueue(db, point)
    flush_queue(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
