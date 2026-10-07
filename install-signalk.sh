#!/usr/bin/env bash
# OPTIONNEL — Signal K sur le Pi : tableau de bord des instruments sur le WiFi
# du bord, liste des appareils NMEA 2000, et source de secours pour le tracker.
# Le tracker n'en a PAS besoin : il lit directement la passerelle YDEN-03.
#
#   bash install-signalk.sh <hôte-ou-IP de la passerelle> [raw|nmea0183]
#
#   raw      (défaut) : NMEA 2000 complet. Nécessite d'avoir activé, dans
#            l'interface web du YDEN (http://yden.local, mot de passe admin),
#            un serveur TCP sur le port 1457 avec le protocole « NMEA 2000 (RAW) ».
#   nmea0183 : utilise le serveur n°1 usine (TCP 1456), aucun réglage YDEN.
set -euo pipefail

GATEWAY="${1:-yden.local}"
MODE="${2:-raw}"
CONFIG_DIR="$HOME/.signalk"

if [[ $EUID -eq 0 ]]; then echo "Lance ce script avec ton utilisateur normal, pas avec sudo." >&2; exit 1; fi

echo "==> Node.js 24 (NodeSource) et dépendances mDNS"
if ! command -v node >/dev/null || [[ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 22 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libnss-mdns avahi-utils libavahi-compat-libdnssd-dev >/dev/null
node -v; npm -v

echo "==> Signal K server"
sudo npm install -g --no-fund --no-audit signalk-server

if [[ ! -f /etc/systemd/system/signalk.service ]]; then
  echo "==> Assistant Signal K (répondre : nom du bateau, MMSI si connu, port 80 : Non, SSL : Non)"
  sudo signalk-server-setup
fi

echo "==> Connexion vers la passerelle ($GATEWAY, mode $MODE)"
python3 - "$CONFIG_DIR/settings.json" "$GATEWAY" "$MODE" <<'PY'
import json, sys
path, gateway, mode = sys.argv[1:4]
settings = json.load(open(path))
if mode == "raw":
    provider = {"id": "yden", "enabled": True, "pipeElements": [{"type": "providers/simple", "options": {
        "logging": False, "type": "NMEA2000",
        "subOptions": {"type": "ydwg02-canboatjs", "host": gateway, "port": "1457", "useCanName": True}}}]}
else:
    provider = {"id": "yden", "enabled": True, "pipeElements": [{"type": "providers/simple", "options": {
        "logging": False, "type": "NMEA0183",
        "subOptions": {"type": "tcp", "host": gateway, "port": "1456"}}}]}
providers = [p for p in settings.get("pipedProviders", []) if p.get("id") != "yden"]
providers.append(provider)
settings["pipedProviders"] = providers
json.dump(settings, open(path, "w"), indent=2)
print("    settings.json mis à jour")
PY

echo "==> Plugin vent vrai / cap vrai (signalk-derived-data)"
( cd "$CONFIG_DIR" && npm install --no-fund --no-audit signalk-derived-data >/dev/null )
mkdir -p "$CONFIG_DIR/plugin-config-data"
cat > "$CONFIG_DIR/plugin-config-data/derived-data.json" <<'JSON'
{
  "enabled": true,
  "enableLogging": false,
  "configuration": {
    "wind": { "groundWind": true },
    "heading": { "heading": true, "magneticVariation": true, "cog_true": true }
  }
}
JSON

sudo systemctl restart signalk.service
echo
echo "Signal K installé. Interface : http://$(hostname).local:3000  (créer l'admin au premier accès)"
echo "Vérifier dans Data Browser : navigation.position, navigation.speedOverGround, environment.wind.*"
echo "Pour en faire la source de secours du tracker : SIGNALK_URL=http://localhost:3000 dans ~/pfp-tracker/config.env"
