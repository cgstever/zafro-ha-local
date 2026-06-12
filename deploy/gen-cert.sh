#!/usr/bin/env bash
# Generate the self-signed cert the AC connects to. The AC does NOT validate it,
# so the CN/SAN can stay as the vendor hostname regardless of your setup.
set -e
DIR=/etc/zafro-cloud
mkdir -p "$DIR"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$DIR/key.pem" -out "$DIR/cert.pem" -days 3650 \
  -subj "/CN=zafro.nbrowan.com" \
  -addext "subjectAltName=DNS:zafro.nbrowan.com"
chmod 600 "$DIR/key.pem"
echo "Wrote $DIR/cert.pem and $DIR/key.pem"
