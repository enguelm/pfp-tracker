#!/usr/bin/env python3
"""Vent à 10 m du modèle NOAA GFS → une image PNG + ses métadonnées JSON.

Lancé toutes les 6 h par la GitHub Action `wind.yml` ; le site lit le résultat
pour animer les particules de vent sur la carte du direct.

Encodage (même principe que mapbox/webgl-wind) :
  - wind.png  : R = composante u (vers l'est), G = composante v (vers le nord),
                normalisées linéairement de [min, max] vers [0, 255]. B = 0.
                Ligne 0 = 90°N, colonne 0 = 180°W (recentré, pas 0°E comme GFS),
                pas de 0,5°.
  - wind.json : dimensions, bornes min/max, heure de validité, source.

Données publiques NOAA (domaine public), sans clé :
https://nomads.ncep.noaa.gov/ — filtre GRIB pour ne télécharger que u/v à 10 m.

Usage : python build_wind.py OUT_DIR
Dépendances : eccodes, numpy, pillow.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
from PIL import Image

FILTER = (
    "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p50.pl"
    "?dir=%2Fgfs.{day}%2F{run:02d}%2Fatmos"
    "&file=gfs.t{run:02d}z.pgrb2full.0p50.f{fh:03d}"
    "&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on"
)


def latest_available(now: datetime) -> tuple[bytes, datetime]:
    """Le run le plus récent publié (il sort ~4 h après l'heure nominale).

    On prend l'échéance +3 h des runs récents plutôt que l'analyse : elle
    colle mieux à « maintenant » quand le run a quelques heures.
    """
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= timedelta(hours=base.hour % 6)
    for back in range(0, 5):
        run_time = base - timedelta(hours=6 * back)
        url = FILTER.format(day=run_time.strftime("%Y%m%d"), run=run_time.hour, fh=3)
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                data = res.read()
        except Exception as exc:  # 404 tant que le run n'est pas publié
            print(f"run {run_time:%Y-%m-%d %Hz} indisponible : {exc}", file=sys.stderr)
            continue
        if data[:4] == b"GRIB":
            return data, run_time
    raise SystemExit("Aucun run GFS disponible sur les 24 dernières heures.")


def decode(grib: bytes) -> tuple[np.ndarray, np.ndarray, datetime]:
    fields: dict[str, np.ndarray] = {}
    valid: datetime | None = None
    tmp = Path("wind.grb2")
    tmp.write_bytes(grib)
    with tmp.open("rb") as f:
        while (gid := eccodes.codes_grib_new_from_file(f)) is not None:
            try:
                name = eccodes.codes_get(gid, "shortName")  # 10u / 10v
                ni = eccodes.codes_get(gid, "Ni")
                nj = eccodes.codes_get(gid, "Nj")
                values = eccodes.codes_get_values(gid).reshape(nj, ni)
                fields[name] = values
                vd = str(eccodes.codes_get(gid, "validityDate"))
                vt = int(eccodes.codes_get(gid, "validityTime"))
                valid = datetime(int(vd[:4]), int(vd[4:6]), int(vd[6:]), vt // 100, tzinfo=timezone.utc)
            finally:
                eccodes.codes_release(gid)
    tmp.unlink()
    if "10u" not in fields or "10v" not in fields or valid is None:
        raise SystemExit(f"Champs inattendus dans le GRIB : {sorted(fields)}")
    return fields["10u"], fields["10v"], valid


def to_png(u: np.ndarray, v: np.ndarray, out_dir: Path, valid: datetime, run: datetime) -> None:
    # GFS : ligne 0 = 90°N, colonne 0 = 0°E. On recentre sur 180°W pour que
    # le Pacifique ne soit pas coupé au milieu de l'image.
    half = u.shape[1] // 2
    u = np.roll(u, half, axis=1)
    v = np.roll(v, half, axis=1)

    meta = {
        "source": "NOAA GFS 0.5° — vent à 10 m",
        "run": run.isoformat(),
        "valid": valid.isoformat(),
        "width": int(u.shape[1]),
        "height": int(u.shape[0]),
        "lonMin": -180.0,
        "latMax": 90.0,
        "step": 0.5,
        "uMin": float(np.floor(u.min())),
        "uMax": float(np.ceil(u.max())),
        "vMin": float(np.floor(v.min())),
        "vMax": float(np.ceil(v.max())),
    }

    def scale(a: np.ndarray, lo: float, hi: float) -> np.ndarray:
        return np.clip(np.round((a - lo) / (hi - lo) * 255), 0, 255).astype(np.uint8)

    rgb = np.zeros((*u.shape, 3), dtype=np.uint8)
    rgb[..., 0] = scale(u, meta["uMin"], meta["uMax"])
    rgb[..., 1] = scale(v, meta["vMin"], meta["vMax"])

    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, "RGB").save(out_dir / "wind.png", optimize=True)
    (out_dir / "wind.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    grib, run = latest_available(datetime.now(timezone.utc))
    u, v, valid = decode(grib)
    to_png(u, v, Path(sys.argv[1]), valid, run)


if __name__ == "__main__":
    main()
