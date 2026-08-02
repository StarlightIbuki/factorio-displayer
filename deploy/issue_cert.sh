#!/usr/bin/env bash
# Issue / renew a Let's Encrypt certificate via the Aliyun DNS (alidns) API
# using the DNS-01 challenge — no inbound ports 80/443 required.
#
# The domain DNS stays on Aliyun; we only use its API to create the
# _acme-challenge TXT record. The cert itself comes from Let's Encrypt.
#
# Run AS YOUR NORMAL USER (do NOT use sudo — acme.sh lives in ~/.acme.sh and
# sudo strips the Aliyun keys). The script uses sudo internally only to copy
# the cert into /etc and reload Caddy.
#
#   export Ali_Key='<AccessKey ID>'
#   export Ali_Secret='<AccessKey Secret>'
#   bash /tmp/issue_cert.sh
#
# Alternatively, drop the keys in /tmp/ali.env (chmod 600) and just run:
#   bash /tmp/issue_cert.sh
#
set -euo pipefail

DOMAIN="${DOMAIN:-factorio.qvq.moe}"
CERT_DIR=/etc/factorio-display/ssl
CA="${CA:-letsencrypt}"
ACME="${ACME:-$HOME/.acme.sh/acme.sh}"

# Keys may come from the environment, or from a 0600 file the owner created.
if [ -f /tmp/ali.env ]; then
  # shellcheck disable=SC1091
  . /tmp/ali.env
fi
# acme.sh runs as a subprocess and only inherits EXPORTED variables.
export Ali_Key Ali_Secret

if [ -z "${Ali_Key:-}" ] || [ -z "${Ali_Secret:-}" ]; then
  echo "Ali_Key / Ali_Secret are not set." >&2
  echo "Either export them first, or create /tmp/ali.env with:" >&2
  echo "  Ali_Key='...'  Ali_Secret='...'" >&2
  exit 1
fi

if [ ! -x "$ACME" ]; then
  echo "acme.sh not found at $ACME — install it first (curl https://get.acme.sh | sh)." >&2
  exit 1
fi

echo "==> issuing $DOMAIN (dns_ali / $CA)"
"$ACME" --issue --dns dns_ali -d "$DOMAIN" --server "$CA"

echo "==> installing cert (staged, then root-copied to $CERT_DIR)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
"$ACME" --install-cert -d "$DOMAIN" \
  --key-file "$STAGE/key.pem" \
  --fullchain-file "$STAGE/fullchain.pem" \
  --server "$CA"

sudo mkdir -p "$CERT_DIR"
sudo install -m 644 "$STAGE/fullchain.pem" "$CERT_DIR/fullchain.pem"
sudo install -m 600 "$STAGE/key.pem" "$CERT_DIR/key.pem"
sudo systemctl reload caddy 2>/dev/null || echo "note: reload caddy when it is running"
echo "==> cert ready"
sudo ls -la "$CERT_DIR"
