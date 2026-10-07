#!/usr/bin/env bash
# Installation du PFP tracker sur le Raspberry Pi (Raspberry Pi OS Lite).
# À lancer depuis le dossier qui contient ces fichiers, avec l'utilisateur
# courant (pas root) :   bash install.sh
# Idempotent : relançable sans risque après une mise à jour des fichiers.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Lance ce script avec ton utilisateur normal, pas avec sudo." >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$HOME"
TARGET="$HOME_DIR/pfp-tracker"

echo "==> Paquets système (python3, sqlite3, mDNS pour yden.local)"
if sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 sqlite3 avahi-daemon libnss-mdns >/dev/null; then
  echo "    paquets OK"
else
  echo "    AVERTISSEMENT : paquets non installés (pas d'Internet ?). python3 suffit au tracker ;"
  echo "    relancer install.sh plus tard pour sqlite3 et la résolution de yden.local."
fi

echo "==> Fichiers du tracker dans $TARGET"
mkdir -p "$TARGET"
if [[ "$HERE" != "$TARGET" ]]; then
  install -m 755 "$HERE/tracker.py" "$TARGET/tracker.py"
  install -m 755 "$HERE/check.sh" "$TARGET/check.sh"
else
  chmod 755 "$TARGET/tracker.py" "$TARGET/check.sh"
fi
if [[ ! -f "$TARGET/config.env" ]]; then
  install -m 600 "$HERE/config.example.env" "$TARGET/config.env"
  echo "    config.env créé depuis l'exemple — à compléter (TRACKER_TOKEN) quand Supabase sera en ligne."
else
  echo "    config.env existant conservé."
fi

echo "==> Unités systemd (utilisateur $USER_NAME)"
sed -e "s|@USER@|$USER_NAME|g" -e "s|@HOME@|$HOME_DIR|g" "$HERE/pfp-tracker.service" \
  | sudo tee /etc/systemd/system/pfp-tracker.service >/dev/null
sudo install -m 644 "$HERE/pfp-tracker.timer" /etc/systemd/system/pfp-tracker.timer
sudo systemctl daemon-reload
sudo systemctl enable --now pfp-tracker.timer

echo "==> Heure : fuseau UTC et synchro NTP (le Pi n'a pas d'horloge sauvegardée)"
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true

echo
echo "Installation terminée."
echo "  Prochain passage : $(systemctl list-timers pfp-tracker.timer --no-pager | sed -n 2p)"
echo "  Diagnostic complet :   bash $TARGET/check.sh"
echo "  Test immédiat (60 s) : bash $TARGET/check.sh --sample"
