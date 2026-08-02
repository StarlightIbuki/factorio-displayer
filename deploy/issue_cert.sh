#!/usr/bin/env bash
# Issue / renew a Let's Encrypt certificate for the backend using the Aliyun
# DNS (alidns) API for DNS-01 validation — no inbound ports 80/443 required.
#
# The domain DNS stays on Aliyun; we only use its API to create the
# _acme-challenge TXT record. The cert itself comes from Let's Encrypt.
#
# Run ON THE SERVER (e.g. `ssh tc`). Type your Aliyun API key yourself:
#
#   export Ali_Key='<Aliyun AccessKey ID>'
#   export Ali_Secret='<Aliyun AccessKey Secret>'
#   bash /tmp/issue_cert.sh
#
# (The key only needs the AliyunDNS FullControl permission. acme.sh stores it
# so future auto-renewals via cron work without re-entering it.)
#
set -euo pipefail

DOMAIN="${DOMAIN:-factorio.qvq.moe}"
CERT_DIR=/etc/factorio-display/ssl
CA="${CA:-letsencrypt}"

if [ -z "${Ali_Key:-}" ] || [ -z "${Ali_Secret:-}" ]; then
  echo "Ali_Key / Ali_Secret must be set (Aliyun DNS API key)." >&2
  exit 1
fi

echo "==> issuing $DOMAIN (dns_ali / $CA)"
~/.acme.sh/acme.sh --issue --dns dns_ali -d "$DOMAIN" --server "$CA"

echo "==> installing cert to $CERT_DIR"
sudo mkdir -p "$CERT_DIR"
~/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
  --key-file "$CERT_DIR/key.pem" \
  --fullchain-file "$CERT_DIR/fullchain.pem" \
  --reloadcmd "sudo systemctl reload caddy" \
  --server "$CA"

echo "==> cert ready: $CERT_DIR/fullchain.pem"
ls -la "$CERT_DIR"
