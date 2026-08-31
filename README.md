# PFP tracker — Raspberry Pi du bord

Relevé de navigation (position, vitesse, cap, vent) depuis le réseau NMEA du
bateau vers le site, via Starlink. Toutes les 30 minutes, un point part vers
l'Edge Function Supabase `ingest-position` ; le site l'affiche sur la page
« Le direct » (`/direct`). Tant qu'aucun relevé n'existe, le site montre le
bateau au port de Papeete avec les données nav en tirets.

## Architecture

```
NMEA 2000 ──> gateway WiFi ──> Signal K (Raspberry Pi) ──> tracker.py ──> Starlink ──> Supabase ──> site
```

- **Signal K** (recommandé via [OpenPlotter](https://openplotter.readthedocs.io/)) fait
  tout le parsing NMEA 2000/0183 et expose les données proprement en
  `http://localhost:3000`. C'est la couche éprouvée de la communauté.
- **tracker.py** échantillonne 6 relevés sur ~60 s, agrège (médiane pour les
  vitesses, moyenne circulaire pour les angles), met le point en file locale
  SQLite, puis vide la file par batchs de 100.
- **Coupure Starlink** : les points s'accumulent dans la file (~100 jours de
  marge à 30 min) et repartent au retour du lien. L'ingestion est idempotente
  (upsert sur `ts`) : un batch renvoyé deux fois n'insère rien de plus. Côté
  site, le panneau affiche toujours « dernier relevé il y a X » — au-delà de
  24 h sans nouvelles, il passe en « dernière position connue ».

## Prérequis

1. Raspberry Pi (3B+ ou mieux) sous Raspberry Pi OS ou OpenPlotter, connecté
   au WiFi de la gateway NMEA **et** à Internet (Starlink).
2. Signal K opérationnel avec la gateway déclarée comme source (vérifier dans
   l'UI Signal K → Data Browser que `navigation.position`,
   `navigation.speedOverGround` et `environment.wind.*` remontent).
   ⚠️ Vérifier au ponton que la gateway expose bien le bus en TCP/UDP — les
   écosystèmes Garmin sont souvent propriétaires ; les gateways dédiées
   (Yacht Devices YDWG-02, Actisense W2K-1) fonctionnent nativement.
3. Le `TRACKER_TOKEN` (secret défini côté Supabase, voir plus bas).

## Installation

```bash
mkdir -p /home/pi/pfp-tracker
cp tracker.py config.example.env /home/pi/pfp-tracker/
mv /home/pi/pfp-tracker/config.example.env /home/pi/pfp-tracker/config.env
nano /home/pi/pfp-tracker/config.env        # renseigner TRACKER_TOKEN

# Optionnel, uniquement pour le fallback NMEA0183 TCP sans Signal K :
pip install pynmea2

sudo cp pfp-tracker.service pfp-tracker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pfp-tracker.timer
```

## Vérification

```bash
systemctl list-timers pfp-tracker.timer          # prochaine exécution
journalctl -u pfp-tracker -n 50                  # logs du dernier run
sqlite3 /home/pi/pfp-tracker/queue.db 'select count(*) from queue;'   # points en attente (0 si tout part)
```

Test à blanc sans bateau (point factice directement vers Supabase) :

```bash
source /home/pi/pfp-tracker/config.env
curl -i -X POST "$SUPABASE_FUNCTION_URL" \
  -H "content-type: application/json" -H "x-tracker-token: $TRACKER_TOKEN" \
  -d '{"points":[{"ts":"2026-11-15T12:00:00Z","lat":-17.535,"lon":-149.5677,"sog_kn":6.4,"cog_deg":312,"tws_kn":14.2,"twd_deg":85}]}'
# Attendu : 200 {"received":1,"inserted":1} — puis penser à nettoyer la table.
```

## Côté Supabase (une fois, depuis un poste connecté)

```bash
npx supabase login
npx supabase link --project-ref uiblqthfbysfxjalwdzw
npx supabase db push                                        # migration boat_positions
npx supabase secrets set TRACKER_TOKEN="$(openssl rand -hex 32)"   # le noter pour config.env
npx supabase functions deploy ingest-position
```
