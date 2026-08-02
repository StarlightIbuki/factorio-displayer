#!/usr/bin/env bash
# Issue an access token using the server's stored token key.
# Usage: bash /tmp/issue_token.sh <user> [ttl-hours]
set -euo pipefail
USER="${1:-default}"
TTL="${2:-720}"
KEY="$(sudo grep FACTORIO_TOKEN_KEY /etc/factorio-display/env | cut -d= -f2)"
cd /opt/factorio-display
./venv/bin/python -m factorio_display token issue --key "$KEY" --user "$USER" --ttl-hours "$TTL"
