#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

BROADLINK_IP="$(bashio::config 'broadlink_ip')"
BROADLINK_MAC="$(bashio::config 'broadlink_mac')"
export BROADLINK_IP BROADLINK_MAC
export DATA_DIR="/data"

if [ -z "${BROADLINK_IP}" ]; then
    bashio::log.warning "Option 'broadlink_ip' ist leer – bitte IP des RM4 mini eintragen."
fi

bashio::log.info "Starte Broadlink IR Manager (RM4 mini: ${BROADLINK_IP:-nicht gesetzt})"

cd /app
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips "*" \
    --log-level info \
    --no-access-log
