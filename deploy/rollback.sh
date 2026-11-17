#!/usr/bin/env bash
# ==============================================================================
# Jarvis Maps AI — Production Database Rollback & Recovery Script
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/jarvismaps"
BACKUP_DIR="${APP_DIR}/server/backups"
DB_FILE="${APP_DIR}/server/licenses.db"
SERVICE_NAME="jarvis-license.service"

echo "============================================================"
echo "  Jarvis Maps AI — Rollback & Disaster Recovery"
echo "============================================================"

if [[ $EUID -ne 0 ]]; then
   echo "[-] ERROR: This script must be run as root (use sudo)." >&2
   exit 1
fi

if [[ ! -d "${BACKUP_DIR}" ]]; then
    echo "[-] No backup directory found at ${BACKUP_DIR}." >&2
    exit 1
fi

echo "[*] Available SQLite backups:"
BACKUPS=($(ls -t "${BACKUP_DIR}"/*.db 2>/dev/null || true))

if [[ ${#BACKUPS[@]} -eq 0 ]]; then
    echo "[-] No backup files available in ${BACKUP_DIR}." >&2
    exit 1
fi

LATEST_BACKUP="${BACKUPS[0]}"
TARGET_BACKUP="${1:-$LATEST_BACKUP}"

if [[ ! -f "${TARGET_BACKUP}" ]]; then
    echo "[-] Selected backup file does not exist: ${TARGET_BACKUP}" >&2
    exit 1
fi

echo "[*] Selected backup for restoration: ${TARGET_BACKUP}"
read -p "Are you sure you want to restore this backup? [y/N]: " CONFIRM
if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
    echo "[-] Rollback aborted by user."
    exit 0
fi

echo "[1/4] Stopping licensing service..."
systemctl stop ${SERVICE_NAME}

echo "[2/4] Creating pre-rollback emergency snapshot..."
SNAPSHOT="${BACKUP_DIR}/licenses_emergency_before_rollback_$(date +%Y%m%d_%H%M%S).db"
if [[ -f "${DB_FILE}" ]]; then
    cp "${DB_FILE}" "${SNAPSHOT}"
    echo "[+] Current state saved to: ${SNAPSHOT}"
fi

echo "[3/4] Restoring database from backup..."
cp "${TARGET_BACKUP}" "${DB_FILE}"
chown www-data:www-data "${DB_FILE}" 2>/dev/null || true

echo "[4/4] Restarting licensing service and verifying health..."
systemctl start ${SERVICE_NAME}
sleep 2

HEALTH=$(curl -s http://127.0.0.1:8081/api/health || true)
if echo "${HEALTH}" | grep -q '"status": "online"'; then
    echo "[+] SUCCESS: Service restored and healthy!"
    echo "    Health Response: ${HEALTH}"
else
    echo "[-] WARNING: Health check failed after restore. Check logs: journalctl -u ${SERVICE_NAME} -n 20"
fi
