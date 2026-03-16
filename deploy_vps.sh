#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  PhotoFinder – Hostinger VPS Deploy Script
#  Run this ONCE on your VPS:  bash deploy_vps.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

APP_DIR="/var/www/PhotoFinder"
SERVICE="photofinder"
REPO="https://github.com/zayroninfotech/PhotoFinder.git"

echo ""
echo "============================================"
echo "  PhotoFinder – VPS Setup"
echo "============================================"

# 1. System packages
echo "[1/8] Installing system packages..."
apt update -qq
apt install -y -qq python3 python3-pip python3-venv git nginx \
    libgl1 libglib2.0-0 curl gnupg

# 2. MongoDB
echo "[2/8] Installing MongoDB..."
if ! command -v mongod &>/dev/null; then
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
        gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
    echo "deb [ arch=amd64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] \
https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | \
        tee /etc/apt/sources.list.d/mongodb-org-7.0.list
    apt update -qq && apt install -y -qq mongodb-org
fi
systemctl enable mongod && systemctl start mongod
echo "  ✓ MongoDB running"

# 3. Clone / update repo
echo "[3/8] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull origin main
else
    git clone "$REPO" "$APP_DIR"
    cd "$APP_DIR"
fi

# 4. Python venv + deps
echo "[4/8] Installing Python packages..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Packages installed"

# 5. Create log directory
mkdir -p /var/log/photofinder

# 6. Systemd service
echo "[5/8] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE}.service << EOF
[Unit]
Description=PhotoFinder Flask App
After=network.target mongod.service

[Service]
User=root
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/venv/bin"
ExecStart=${APP_DIR}/venv/bin/gunicorn -c ${APP_DIR}/gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE
echo "  ✓ Service started"

# 7. Nginx config
echo "[6/8] Configuring Nginx..."
SERVER_IP=$(hostname -I | awk '{print $1}')
cat > /etc/nginx/sites-available/$SERVICE << EOF
server {
    listen 80;
    server_name ${SERVER_IP} _;

    client_max_body_size 500M;
    proxy_read_timeout   300s;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$SERVICE /etc/nginx/sites-enabled/$SERVICE
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "  ✓ Nginx configured"

# 8. Firewall
echo "[7/8] Opening firewall..."
ufw allow 22 && ufw allow 80 && ufw allow 443
ufw --force enable
echo "  ✓ Firewall configured"

echo ""
echo "============================================"
echo "  ✅ DEPLOY COMPLETE!"
echo "  App running at: http://${SERVER_IP}"
echo "  Service:  systemctl status $SERVICE"
echo "  Logs:     journalctl -u $SERVICE -f"
echo "============================================"
echo ""
echo "  ⚠  Don't forget to edit config.py:"
echo "     nano ${APP_DIR}/config.py"
echo "     → Add Razorpay keys"
echo "     → Add Email credentials"
echo "     Then: systemctl restart $SERVICE"
