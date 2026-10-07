#!/usr/bin/env bash
# Diagnostic du tracker sur le Pi. Options :
#   --sample   écoute la passerelle 60 s et affiche le point calculé (sans l'envoyer)
set -uo pipefail

TARGET="$HOME/pfp-tracker"
CONFIG="$TARGET/config.env"
if [[ -f "$CONFIG" ]]; then
  set -a; # shellcheck disable=SC1090
  source "$CONFIG"; set +a
fi
NMEA_TCP_HOST="${NMEA_TCP_HOST:-yden.local}"
NMEA_TCP_PORT="${NMEA_TCP_PORT:-1456}"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
ko()   { printf '  \033[31mKO\033[0m   %s\n' "$*"; }
info() { printf '  --   %s\n' "$*"; }

echo "== Réseau"
GW_IP="$(getent ahostsv4 "$NMEA_TCP_HOST" 2>/dev/null | awk 'NR==1{print $1}')"
if [[ -n "$GW_IP" ]]; then ok "passerelle $NMEA_TCP_HOST → $GW_IP"; else ko "passerelle $NMEA_TCP_HOST introuvable (mDNS ? mettre l'IP dans config.env)"; fi
if [[ -n "$GW_IP" ]] && timeout 5 bash -c "exec 3<>/dev/tcp/$GW_IP/$NMEA_TCP_PORT" 2>/dev/null; then
  ok "port TCP $NMEA_TCP_PORT ouvert sur la passerelle"
  info "5 premières phrases NMEA :"
  timeout 8 bash -c "exec 3<>/dev/tcp/$GW_IP/$NMEA_TCP_PORT; head -n 5 <&3" 2>/dev/null | sed 's/^/         /' || info "(rien reçu : instruments éteints ou serveur n°1 désactivé)"
else
  ko "port TCP $NMEA_TCP_PORT fermé ou passerelle hors ligne"
fi
if curl -fsS -m 8 -o /dev/null https://uiblqthfbysfxjalwdzw.supabase.co/ 2>/dev/null; then ok "Internet (Starlink) joignable"; else ko "pas d'Internet (Starlink éteint ? WAN du RUTM10 ?) — les points s'accumuleront dans la file"; fi

echo "== Heure"
if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then ok "horloge synchronisée NTP : $(date -u +%FT%TZ)"; else ko "horloge NON synchronisée ($(date -u +%FT%TZ)) — l'heure GPS de la passerelle sera utilisée"; fi

echo "== Service"
if systemctl is-active --quiet pfp-tracker.timer; then ok "timer actif — $(systemctl list-timers pfp-tracker.timer --no-pager | sed -n 2p | awk '{print "prochain passage", $1, $2, $3}')"; else ko "timer inactif (bash install.sh)"; fi
if [[ -n "${TRACKER_TOKEN:-}" && -n "${SUPABASE_FUNCTION_URL:-}" ]]; then ok "envoi Supabase configuré"; else info "envoi Supabase pas encore configuré : les points restent en file locale"; fi
if [[ -f "$TARGET/queue.db" ]]; then info "points en attente dans la file : $(sqlite3 "$TARGET/queue.db" 'select count(*) from queue;' 2>/dev/null || echo '?')"; else info "file locale pas encore créée (aucun passage effectué)"; fi
info "derniers logs :"
journalctl -u pfp-tracker -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/         /'

if [[ "${1:-}" == "--sample" ]]; then
  echo "== Échantillon (60 s d'écoute, rien n'est envoyé)"
  python3 "$TARGET/tracker.py" --dry-run --window 60 | sed 's/^/         /'
fi
