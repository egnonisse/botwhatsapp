#!/bin/bash
# Deploy BotWhatsApp on VPS
# Usage: bash deploy.sh <vps_ip> <ssh_user>
# Example: bash deploy.sh 169.58.117.185 root

set -e
VPS_IP="${1:?Usage: $0 <vps_ip> <ssh_user>}"
SSH_USER="${2:?Usage: $0 <vps_ip> <ssh_user>}"
APP_DIR="/opt/botwhatsapp"

echo "=== 1. Sync files ==="
ssh -o StrictHostKeyChecking=no ${SSH_USER}@${VPS_IP} "mkdir -p ${APP_DIR}"
rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' --exclude 'data/' --exclude 'logs/' \
      ./ ${SSH_USER}@${VPS_IP}:${APP_DIR}/

echo "=== 2. Install dependencies ==="
ssh -o StrictHostKeyChecking=no ${SSH_USER}@${VPS_IP} "bash -s" << 'REMOTE'
set -e
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3.11-venv 2>/dev/null || true
apt-get install -y -qq curl nginx 2>/dev/null || true

cd /opt/botwhatsapp

# Create venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Create data/logs dirs
mkdir -p data logs

# Copy config from sample if not exists
if [ ! -f config.yaml ]; then
    echo "⚠️ No config.yaml found — create one manually"
fi

echo "✅ Dependencies installed"
REMOTE

echo "=== 3. Setup systemd service ==="
ssh -o StrictHostKeyChecking=no ${SSH_USER}@${VPS_IP} "bash -s" << 'REMOTE'
cat > /etc/systemd/system/botwhatsapp.service << 'EOF'
[Unit]
Description=BotWhatsApp - WhatsApp Sales Chatbot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/botwhatsapp
ExecStart=/opt/botwhatsapp/venv/bin/python /opt/botwhatsapp/server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable botwhatsapp
echo "✅ systemd service created"
REMOTE

echo "=== 4. Setup Cloudflare Tunnel (HTTPS for Meta webhook) ==="
ssh -o StrictHostKeyChecking=no ${SSH_USER}@${VPS_IP} "bash -s" << 'REMOTE'
# Install cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
dpkg -i cloudflared.deb 2>/dev/null || apt-get install -y -qq -f
rm -f cloudflared.deb

# Create tunnel service
cat > /etc/systemd/system/cloudflare-tunnel.service << 'EOF'
[Unit]
Description=Cloudflare Tunnel for BotWhatsApp
After=network.target botwhatsapp.service
Requires=botwhatsapp.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/cloudflared tunnel --url http://localhost:8000 --no-autoupdate
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cloudflare-tunnel
echo "✅ Cloudflare tunnel service created"
REMOTE

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "Start services:"
echo "  ssh ${SSH_USER}@${VPS_IP} 'systemctl start botwhatsapp cloudflare-tunnel'"
echo ""
echo "Get tunnel URL:"
echo "  ssh ${SSH_USER}@${VPS_IP} 'journalctl -u cloudflare-tunnel -n 20 --no-pager | grep -o \"https://.*trycloudflare.com\" | tail -1'"
echo ""
echo "Then update Meta webhook:"
echo "  https://<tunnel-url>/webhook/whatsapp"
echo "  Token: <verify_token défini dans config.yaml>"
echo ""
