#!/usr/bin/env bash
# Idempotent setup for the factorio-display backend on a fresh Ubuntu host.
#
#   - copies/starts the backend (uvicorn) as a systemd service behind Caddy
#   - Caddy terminates TLS for $DOMAIN and reverse-proxies to 127.0.0.1:8000
#   - generates & stores the HMAC token key at /etc/factorio-display/env
#
# Usage (from your machine, after `scp`'ing the built wheel to $APP_DIR):
#   scp dist/factorio_display-0.1.0-py3-none-any.whl tc:/opt/factorio-display/
#   scp deploy/setup_server.sh tc:/tmp/
#   ssh tc "sudo bash /tmp/setup_server.sh"
#
set -euo pipefail

DOMAIN="${DOMAIN:-factorio.qvq.moe}"
APP_DIR="${APP_DIR:-/opt/factorio-display}"
SECRET_DIR=/etc/factorio-display
SERVICE_USER="${SERVICE_USER:-ubuntu}"

echo "==> directories"
sudo mkdir -p "$APP_DIR/data" "$SECRET_DIR"
sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

echo "==> token key (kept if it already exists)"
if [ ! -s "$SECRET_DIR/env" ]; then
  KEY=$(openssl rand -hex 32)
  echo "FACTORIO_TOKEN_KEY=$KEY" | sudo tee "$SECRET_DIR/env" >/dev/null
fi
sudo chmod 600 "$SECRET_DIR/env"

echo "==> Caddyfile (HTTPS on high port 60012, manual cert from issue_cert.sh)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
factorio.qvq.moe:60012 {
	tls /etc/factorio-display/ssl/fullchain.pem /etc/factorio-display/ssl/key.pem
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000
}
EOF

echo "==> systemd unit"
sudo tee /etc/systemd/system/factorio-display.service >/dev/null <<'EOF'
[Unit]
Description=factorio-display API server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/factorio-display
EnvironmentFile=/etc/factorio-display/env
ExecStart=/opt/factorio-display/venv/bin/python -m factorio_display server --host 127.0.0.1 --port 8000 --data-dir /opt/factorio-display/data --max-workers 2 --max-jobs-per-user 5 --token-key ${FACTORIO_TOKEN_KEY}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> enable & start the backend"
sudo systemctl daemon-reload
sudo systemctl enable --now factorio-display
sleep 3
systemctl is-active factorio-display

# Caddy only starts once a cert exists (see issue_cert.sh).
if [ -f /etc/factorio-display/ssl/fullchain.pem ]; then
  sudo systemctl enable --now caddy
  systemctl is-active caddy
else
  echo "note: no certificate yet — run issue_cert.sh to get one, then:"
  echo "      sudo systemctl enable --now caddy"
fi

echo "==> your token key (use it with: factorio-display token issue --key <this> --user <name>)"
grep FACTORIO_TOKEN_KEY "$SECRET_DIR/env"
