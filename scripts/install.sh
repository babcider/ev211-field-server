#!/usr/bin/env bash
# Ubuntu 서버에 EV211 Field Server 설정·systemd 서비스·운영 명령을 설치한다
set -euo pipefail

APP_DIR="/opt/ev211-field-server"
ENV_DIR="/etc/ev211-field"
ENV_FILE="$ENV_DIR/ev211.env"
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "sudo ./scripts/install.sh 로 실행하세요." >&2
  exit 1
fi

[ "$(uname -s)" = "Linux" ] || { echo "Ubuntu Linux에서만 설치할 수 있습니다." >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || { echo "현재 지원 운영체제는 Ubuntu입니다." >&2; exit 1; }

for command in docker curl openssl git ip tar; do
  command -v "$command" >/dev/null || {
    echo "필수 명령이 없습니다: $command" >&2
    exit 1
  }
done
docker compose version >/dev/null
docker info >/dev/null

if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
  if [ -d "$APP_DIR" ] && [ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "$APP_DIR 가 비어 있지 않습니다. 기존 설치를 확인하세요." >&2
    exit 1
  fi
  install -d -m 755 "$APP_DIR"
  tar -C "$SOURCE_DIR" \
    --exclude='./.env' --exclude='./.venv' --exclude='__pycache__' \
    --exclude='.pytest_cache' -cf - . | tar -C "$APP_DIR" -xf -
fi

install -d -m 700 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  NODE_IP="$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$NODE_IP" ] || { echo "서버 LAN IP를 감지하지 못했습니다." >&2; exit 1; }
  LIVEKIT_API_KEY="field_$(openssl rand -hex 6)"
  LIVEKIT_API_SECRET="$(openssl rand -hex 32)"
  SEND_PASSWORD="send_$(openssl rand -hex 12)"
  ADMIN_PASSWORD="admin_$(openssl rand -hex 12)"

  umask 077
  {
    echo "LIVEKIT_API_KEY=$LIVEKIT_API_KEY"
    echo "LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET"
    echo "LIVEKIT_KEYS=\"$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET\""
    echo "FIELD_NODE_IP=$NODE_IP"
    echo "FIELD_WS_URL=ws://$NODE_IP:7880"
    echo "FIELD_SEND_PASSWORD=$SEND_PASSWORD"
    echo "FIELD_ADMIN_PASSWORD=$ADMIN_PASSWORD"
    echo "FIELD_UDP_START=50000"
    echo "FIELD_UDP_END=60000"
    echo "MAX_CHANNELS=15"
    echo "FIELD_DB_PATH=/data/field.db"
    echo "FIELD_RECORDINGS_PATH=/data/recordings"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "기존 설정을 유지합니다: $ENV_FILE"
fi

install -m 755 "$APP_DIR/scripts/ev211ctl" /usr/local/bin/ev211ctl
install -m 644 "$APP_DIR/systemd/ev211-field.service" /etc/systemd/system/ev211-field.service
install -d -m 700 /var/backups/ev211-field

docker compose \
  --project-directory "$APP_DIR" \
  --env-file "$ENV_FILE" \
  -f "$APP_DIR/docker-compose.yml" \
  -f "$APP_DIR/docker-compose.host.yml" config >/dev/null

systemctl daemon-reload
systemctl enable --now ev211-field.service
/usr/local/bin/ev211ctl doctor

echo
echo "설치가 완료되었습니다."
echo "접속 정보는 sudo ev211ctl credentials 로 확인하세요."
echo "방화벽과 DHCP 예약은 docs/INSTALL.md 안내에 따라 설정하세요."
