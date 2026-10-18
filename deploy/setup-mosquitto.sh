#!/usr/bin/env bash
set -euo pipefail

# Run on a fresh Ubuntu VM as root: sudo bash setup-mosquitto.sh PUBLIC_IP
BROKER_IP="${1:?Pass the VM public IP as the first argument}"
if [[ "$EUID" -ne 0 ]]; then echo "Run with sudo" >&2; exit 1; fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y mosquitto mosquitto-clients openssl ufw
install -d -m 750 -o root -g mosquitto /etc/mosquitto/certs

openssl genrsa -out /etc/mosquitto/certs/ca.key 4096
openssl req -x509 -new -nodes -key /etc/mosquitto/certs/ca.key -sha256 -days 365 \
  -subj '/CN=Wheelworks MQTT Demo CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -out /etc/mosquitto/certs/ca.crt
openssl genrsa -out /etc/mosquitto/certs/server.key 2048
openssl req -new -key /etc/mosquitto/certs/server.key -subj '/CN=Wheelworks MQTT Broker' \
  -out /etc/mosquitto/certs/server.csr
cat > /etc/mosquitto/certs/server.ext <<EOF
subjectAltName=IP:${BROKER_IP}
extendedKeyUsage=serverAuth
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
EOF
openssl x509 -req -in /etc/mosquitto/certs/server.csr \
  -CA /etc/mosquitto/certs/ca.crt -CAkey /etc/mosquitto/certs/ca.key \
  -CAcreateserial -out /etc/mosquitto/certs/server.crt -days 365 -sha256 \
  -extfile /etc/mosquitto/certs/server.ext
chown mosquitto:mosquitto /etc/mosquitto/certs/server.key /etc/mosquitto/certs/server.crt /etc/mosquitto/certs/ca.crt
chmod 640 /etc/mosquitto/certs/server.key
chmod 600 /etc/mosquitto/certs/ca.key
chmod 644 /etc/mosquitto/certs/ca.crt

cat > /etc/mosquitto/conf.d/wheelworks.conf <<'EOF'
per_listener_settings true
listener 8883 0.0.0.0
protocol mqtt
allow_anonymous false
password_file /etc/mosquitto/passwd
cafile /etc/mosquitto/certs/ca.crt
certfile /etc/mosquitto/certs/server.crt
keyfile /etc/mosquitto/certs/server.key
EOF

if [[ -t 0 ]]; then
  read -r -s -p 'Create a strong MQTT password (16+ characters): ' BROKER_PASSWORD
  echo
else
  IFS= read -r BROKER_PASSWORD
fi
if [[ "${#BROKER_PASSWORD}" -lt 16 ]]; then echo 'MQTT password must be at least 16 characters' >&2; exit 1; fi
umask 077
printf 'wheelworks:%s\n' "$BROKER_PASSWORD" > /etc/mosquitto/passwd
unset BROKER_PASSWORD
chown root:root /etc/mosquitto/passwd
mosquitto_passwd -U /etc/mosquitto/passwd
chown root:mosquitto /etc/mosquitto/passwd
chmod 640 /etc/mosquitto/passwd
ufw allow OpenSSH
ufw allow 8883/tcp
ufw --force enable
systemctl enable --now mosquitto
systemctl restart mosquitto
systemctl is-active --quiet mosquitto
echo "Broker active on ${BROKER_IP}:8883"
echo 'Copy /etc/mosquitto/certs/ca.crt to the local computer as mosquitto-ca.crt.'
