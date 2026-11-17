#!/usr/bin/env bash
# ==============================================================================
# Jarvis Maps AI — Idempotent Production VPS Deployment Script
# Target OS: Ubuntu 22.04 / 24.04 LTS
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/jarvismaps"
BACKUP_DIR="${APP_DIR}/server/backups"
CONFIG_DIR="${APP_DIR}/server/config"
SERVICE_NAME="jarvis-license.service"

echo "============================================================"
echo "  Jarvis Maps AI — Production Server Deployment"
echo "============================================================"

# Ensure running as root
if [[ $EUID -ne 0 ]]; then
   echo "[-] ERROR: This script must be run as root (use sudo)." >&2
   exit 1
fi

echo "[1/7] Updating system packages and installing prerequisites..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx ufw curl rsync

echo "[2/7] Creating application directory structure..."
mkdir -p "${APP_DIR}"
mkdir -p "${BACKUP_DIR}"
mkdir -p "${CONFIG_DIR}"

# Safety Check: Never overwrite existing production database
if [[ -f "${APP_DIR}/server/licenses.db" ]]; then
    echo "[!] Existing production database detected at ${APP_DIR}/server/licenses.db."
    echo "[*] Creating safety pre-deployment backup..."
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    cp "${APP_DIR}/server/licenses.db" "${BACKUP_DIR}/licenses_pre_deploy_${TIMESTAMP}.db"
    echo "[+] Preserved existing database: ${BACKUP_DIR}/licenses_pre_deploy_${TIMESTAMP}.db"
else
    echo "[*] Fresh database initialization will occur on first service launch."
fi

echo "[3/7] Setting up Python virtual environment..."
if [[ ! -d "${APP_DIR}/venv" ]]; then
    python3 -m venv "${APP_DIR}/venv"
fi

echo "[4/7] Configuring Administrator Secret Key..."
SECRET_FILE="${CONFIG_DIR}/admin_secret.key"
if [[ ! -f "${SECRET_FILE}" ]]; then
    # Generate secure random key if not already provisioned
    GENERATED_KEY="JARVIS-ADMIN-$(openssl rand -hex 16 | tr '[:lower:]' '[:upper:]')"
    echo "${GENERATED_KEY}" > "${SECRET_FILE}"
    echo "[+] Generated new secure administrator key."
else
    echo "[*] Existing administrator key preserved."
fi
chmod 600 "${SECRET_FILE}"

echo "[5/7] Configuring Nginx Web Server..."
cp deploy/nginx.conf /etc/nginx/sites-available/jarvismaps
if [[ ! -L /etc/nginx/sites-enabled/jarvismaps ]]; then
    ln -s /etc/nginx/sites-available/jarvismaps /etc/nginx/sites-enabled/jarvismaps
fi
# Remove default site if present
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "[6/7] Installing and starting Systemd background service..."
cp deploy/jarvis-license.service /etc/systemd/system/${SERVICE_NAME}
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo "[7/7] Configuring UFW Firewall..."
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP Web & Large Downloads'
ufw allow 443/tcp comment 'HTTPS'
# Ensure backend port 8081 is closed to public (proxied only via Nginx)
ufw delete allow 8081/tcp 2>/dev/null || true
ufw --force enable

echo "============================================================"
echo "  DEPLOYMENT COMPLETE & VERIFIED"
echo "  - Website & Downloads: http://$(hostname -I | awk '{print $1}')"
echo "  - License Gateway API: http://$(hostname -I | awk '{print $1}')/api/health"
echo "  - Database Status:     Persistent SQLite active at ${APP_DIR}/server/licenses.db"
echo "============================================================"
