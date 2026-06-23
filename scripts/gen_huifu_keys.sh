#!/usr/bin/env bash
#
# Generate the merchant RSA-2048 keypair for the 汇付斗拱 (Huifu) payment
# integration and print the values to configure.
#
# Two keys are involved, and only ONE is generated here:
#   - merchant PRIVATE key  → goes in `.env` as huifu_rsa_private_key
#                             (signs our outbound requests; keep secret)
#   - merchant PUBLIC key   → UPLOAD to the Huifu 商户运营平台 (so Huifu can
#                             verify our requests); never goes in `.env`
#   - Huifu's PUBLIC key    → copy FROM the Huifu console into `.env` as
#                             huifu_rsa_public_key (verifies their replies);
#                             this script does NOT produce it.
#
# Usage:
#   bash scripts/gen_huifu_keys.sh [output_dir]
#
# Writes the PEM files to <output_dir> (default ./huifu_keys, gitignored —
# do NOT commit) and prints the single-line private-key body for `.env`.

set -euo pipefail

OUT_DIR="${1:-huifu_keys}"
PRIV_PEM="$OUT_DIR/huifu_merchant_private.pem"
PUB_PEM="$OUT_DIR/huifu_merchant_public.pem"

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

# PKCS#8 private key ("BEGIN PRIVATE KEY") — the format dg-sdk expects.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$PRIV_PEM"
openssl rsa -in "$PRIV_PEM" -pubout -out "$PUB_PEM" 2>/dev/null
chmod 600 "$PRIV_PEM"

# Single-line base64 body (strip PEM markers + newlines) for the .env value.
priv_oneline="$(grep -v -- '-----' "$PRIV_PEM" | tr -d '\n')"

echo
echo "Keys written to: $OUT_DIR/  (gitignored — do NOT commit)"
echo
echo "1) Put this in backend/.env:"
echo
echo "huifu_rsa_private_key=$priv_oneline"
echo
echo "2) Upload this PUBLIC key to the Huifu 商户运营平台 (RSA 配置 / 密钥管理):"
echo
cat "$PUB_PEM"
echo
echo "3) Copy HUIFU's platform public key FROM the console and set it (single line):"
echo "   huifu_rsa_public_key=<paste Huifu's public key body, markers/newlines stripped>"
echo
