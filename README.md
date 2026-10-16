# PFP tracker — Raspberry Pi du bord

Relevé de navigation (position, vitesse fond, cap, vent vrai) depuis le réseau
NMEA 2000 du bateau vers le site, via Starlink. Toutes les 30 minutes, un point
part vers l'Edge Function Supabase `ingest-position` ; le site l'affiche sur la
page « Le direct » (`/direct`). Tant qu'aucun relevé n'existe, le site montre le
bateau au port de Papeete avec les données nav en tirets.

## Architecture du bord

```
Raymarine (iTC-5, AIS700, EV-1) ── NMEA 2000 ── YDEN-03N ── Ethernet ──┐
                                                                       │
                                      Starlink ── WAN ── RUTM10 (LAN + WiFi du bord)
                                                           │        │
                                                   Raspberry Pi    PC / téléphones
                                                   (Ethernet)
```

- **YDEN-03N** (Yacht Devices) : passerelle NMEA 2000 → Ethernet. En réglage
  usine elle émet un flux **NMEA 0183 sur TCP, port 1456** (serveur n°1), avec
  position, vitesse et cap fond, heure GPS (RMC) et **vent vrai calculé par la
  passerelle** (MWD) à partir du vent apparent et de SOG/cap. Le cap vient du
  compas **EV-1** du pilote (PGN 127250, magnétique) : la passerelle n'en tire
  un cap vrai (HDT, ou HDG avec déclinaison) que si une déclinaison magnétique
  circule sur le bus (traceur Raymarine en général). Pilote éteint = pas de cap :
  la direction du vent n'est alors publiée qu'au-delà de 2 nœuds. Interface
  web : `http://yden.local`, mot de passe `admin`. En DHCP par défaut.
- **RUTM10** (Teltonika) : routeur du bord. Starlink sur le port WAN, tout le
  reste sur le LAN. LAN usine `192.168.1.1/24` — **à changer** (ex.
  `192.168.10.1/24`) car le routeur Starlink utilise le même sous-réseau.
- **Raspberry Pi** : `tracker.py` écoute la passerelle 60 s, agrège, met le point
  en file SQLite, envoie la file par lots vers Supabase. Zéro dépendance hors
  Python standard. Signal K est **optionnel** (tableau de bord, secours).

### Robustesse

- **Coupure Starlink** : les points s'accumulent (~100 jours de marge) et
  repartent au retour du lien. Ingestion idempotente (upsert sur `ts`).
- **Pas d'horloge sur le Pi** : les points sont horodatés avec l'heure GPS du
  flux NMEA. Sans heure GPS et sans synchro NTP, pas de point.
- **Lot refusé par le serveur** : bissection pour isoler le point fautif, qui
  est jeté ; la file ne reste jamais bloquée.
- **Envoi non configuré** (Supabase pas encore déployé) : les points sont tout
  de même collectés et partiront d'un bloc quand `TRACKER_TOKEN` sera renseigné.
- **Capteur absent** (pilote / EV-1 éteint, iTC-5 éteint) : le point part sans
  le champ concerné. Seule la position est indispensable.
- **Passerelle qui décroche en cours d'écoute** : ce qui a été reçu est gardé,
  reconnexion jusqu'à la fin de la fenêtre. Plusieurs adresses possibles
  (`NMEA_TCP_HOST=IP,yden.local`), essayées dans l'ordre.
- **Données corrompues** (`nan`, `inf`, checksum faux, phrase tronquée) :
  ignorées une par une, sans perdre la fenêtre.
- **Base SQLite corrompue** (coupure de courant) : mise de côté
  (`queue.db.corrompu-…`) et recréée au passage suivant.
- **Relevé qui plante** : la file est quand même envoyée, et le timer relance
  un passage 30 min plus tard quoi qu'il arrive.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `tracker.py` | le programme (Python ≥ 3.9, bibliothèque standard) |
| `config.example.env` | modèle de configuration → `~/pfp-tracker/config.env` |
| `pfp-tracker.service` / `.timer` | unités systemd (modèles, `install.sh` les adapte) |
| `install.sh` | installation complète sur le Pi, relançable |
| `check.sh` | diagnostic : passerelle, Internet, heure, timer, file, logs |
| `install-signalk.sh` | optionnel : Signal K + plugin vent vrai |
| `test_tracker.py` | tests unitaires : `python3 -m unittest -v` |

## Récupérer le code

Ce dossier est aussi publié tel quel dans un repo public dédié :
**https://github.com/enguelm/pfp-tracker** (miroir de `tracker/` du repo du
site, poussé avec `git subtree`). Il ne contient aucun secret.

- **Sur le Pi directement** (il a Internet via Starlink), sans git :

  ```bash
  curl -fsSL https://github.com/enguelm/pfp-tracker/archive/refs/heads/main.tar.gz | tar xz
  bash pfp-tracker-main/install.sh
  ```

- **Depuis le PC** : bouton « Code → Download ZIP » sur GitHub, décompresser,
  puis copier le dossier sur la partition `bootfs` de la carte SD (visible sur
  le Pi dans `/boot/firmware/`) ou l'envoyer en `scp` sur le Pi.

Pour publier une mise à jour depuis le repo du site :
`git subtree push --prefix=tracker pfp-tracker main`.

## Installation pas à pas

### 1. Réseau (RUTM10)

1. Se connecter au RUTM10 (`http://192.168.1.1`, `admin`, mot de passe sur
   l'étiquette). Vérifier que le PC a Internet **à travers** le RUTM10 : le port
   où est branché Starlink doit être en rôle **WAN** (Network → Interfaces).
2. Passer le LAN en `192.168.10.1/24` (Network → Interfaces → LAN) pour éviter
   la collision avec le routeur Starlink. Se reconnecter ensuite sur
   `http://192.168.10.1`.
3. Brancher le YDEN-03N et le Pi sur des ports LAN. Dans Network → Interfaces →
   LAN → DHCP → Static leases, fixer leurs adresses (ex. `.20` pour le YDEN,
   `.10` pour le Pi).

### 2. Passerelle YDEN-03N

1. Connecteur : le modèle **N** a une prise NMEA 2000 Micro mâle. Sur un
   backbone SeaTalk NG il faut un câble adaptateur Raymarine (A06035 / A06075 /
   A06078) ; sur un backbone NMEA 2000 standard, un simple câble de spur.
2. Alimenter le bus : la LED jaune du port Ethernet doit clignoter
   irrégulièrement.
3. Depuis le PC : `http://yden.local` (ou son IP). Le serveur n°1 usine
   (TCP 1456, NMEA 0183) suffit au tracker, mais **passer le mode réseau de
   « DHCP Magic » (usine) à « DHCP Client »** : en DHCP Magic, si la passerelle
   démarre avant le RUTM10 (cas typique à la remise sous tension du bord), elle
   devient serveur DHCP en `192.168.4.1` et n'est plus joignable à son adresse
   habituelle. Le serveur n°1 accepte au plus 3 connexions TCP simultanées :
   brancher les autres applications (OpenCPN, tablette) en UDP ou sur un autre
   serveur pour laisser une place au Pi.
4. Test : avec les instruments allumés, le PC doit voir des phrases
   `$YDRMC`, `$YDMWD` sur le port 1456 (`nc yden.local 1456` ou PuTTY en mode
   raw). Sans `$YDRMC` (GPS qui n'émet pas la PGN 129029), le tracker se
   rabat sur `$YDGLL` / `$YDVTG` et date le point avec l'heure NTP du Pi.

### 3. Carte SD

1. Raspberry Pi Imager → Raspberry Pi OS **Lite 64 bits**.
2. Réglages avancés : nom d'hôte `pfp-pi`, utilisateur `pi` + mot de passe,
   **SSH activé**, fuseau UTC. Pas de WiFi : le Pi est en Ethernet.
3. Brancher le Pi en Ethernet sur le RUTM10 et l'alimenter (USB-C, 3 A).

### 4. Premier démarrage et installation

```bash
ssh pi@pfp-pi.local
curl -fsSL https://github.com/enguelm/pfp-tracker/archive/refs/heads/main.tar.gz | tar xz
bash pfp-tracker-main/install.sh
bash ~/pfp-tracker/check.sh --sample      # 60 s : affiche le point calculé
```

Si le Pi n'a pas encore Internet, copier le dossier depuis le PC sur la
partition `bootfs` de la carte et lancer `bash /boot/firmware/pfp-tracker-main/install.sh`.

`install.sh` installe les paquets, copie les fichiers dans `~/pfp-tracker/`,
crée `config.env`, pose les unités systemd, active le timer et met le Pi en UTC.
Une fois le bail statique posé sur le RUTM10, mettre l'IP fixe de la
passerelle en premier dans `~/pfp-tracker/config.env`, en gardant `yden.local`
en secours : `NMEA_TCP_HOST=192.168.10.20,yden.local`.

### 5. Supabase (plus tard, depuis un poste connecté)

```bash
npx supabase login
npx supabase link --project-ref uiblqthfbysfxjalwdzw
npx supabase db push                                             # table boat_positions
npx supabase secrets set TRACKER_TOKEN="$(openssl rand -hex 32)"  # noter la valeur
npx supabase functions deploy ingest-position
npx supabase gen types typescript --linked > src/integrations/supabase/types.ts
```

Puis sur le Pi : renseigner `TRACKER_TOKEN` dans `~/pfp-tracker/config.env`,
et lancer `sudo systemctl start pfp-tracker` pour vider la file tout de suite.

Test à blanc de la fonction, sans bateau :

```bash
curl -i -X POST "https://uiblqthfbysfxjalwdzw.supabase.co/functions/v1/ingest-position" \
  -H "content-type: application/json" -H "x-tracker-token: $TRACKER_TOKEN" \
  -d '{"points":[{"ts":"2026-11-15T12:00:00Z","lat":-17.535,"lon":-149.5677,"sog_kn":6.4,"cog_deg":312,"tws_kn":14.2,"twd_deg":85}]}'
# Attendu : 200 {"received":1,"inserted":1} — puis nettoyer la table.
```

## Exploitation

```bash
bash ~/pfp-tracker/check.sh                       # état général
systemctl list-timers pfp-tracker.timer           # prochaine exécution
journalctl -u pfp-tracker -n 50                   # logs des derniers passages
sqlite3 ~/pfp-tracker/queue.db 'select count(*) from queue;'   # points en attente
sudo systemctl start pfp-tracker                  # forcer un passage maintenant
```

Une fois le Pi configuré et testé, **faire une image de la carte SD** et garder
une carte de rechange à bord.

## Signal K (optionnel)

```bash
bash ~/pfp-tracker-main/install-signalk.sh 192.168.10.20          # NMEA 2000 RAW (port 1457 à activer sur le YDEN)
bash ~/pfp-tracker-main/install-signalk.sh 192.168.10.20 nmea0183 # ou flux usine 1456, sans réglage YDEN
```

Interface sur `http://pfp-pi.local:3000`. Pour en faire la source de secours du
tracker : `SIGNALK_URL=http://localhost:3000` dans `config.env`.
