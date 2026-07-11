#!/bin/sh
# LiveKit 컨테이너 entrypoint — 마운트된 config 템플릿의 ${LIVEKIT_API_KEY}·${LIVEKIT_WEBHOOK_URL} 를
# 환경변수 값으로 치환해 실제 config 를 생성한 뒤 livekit-server 를 실행한다(단일 소스화, 결함 #18).
set -eu

SRC="/etc/livekit.template.yaml"
DST="/etc/livekit.rendered.yaml"

if [ -z "${LIVEKIT_API_KEY:-}" ]; then
  echo "LIVEKIT_API_KEY 가 비어 있습니다. .env 를 확인하세요." >&2
  exit 1
fi

# 웹훅 URL 기본값 = 도커 브리지 DNS 직결. Linux host-mode 오버레이가 루프백 published
# 포트(http://127.0.0.1:8000/...)로 오버라이드한다(host 네트워크에선 도커 DNS 불가).
LIVEKIT_WEBHOOK_URL="${LIVEKIT_WEBHOOK_URL:-http://field-api:8000/livekit/webhook}"

# envsubst 가 없으면(경량 이미지) sed 폴백.
if command -v envsubst >/dev/null 2>&1; then
  export LIVEKIT_API_KEY LIVEKIT_WEBHOOK_URL
  envsubst '${LIVEKIT_API_KEY} ${LIVEKIT_WEBHOOK_URL}' < "$SRC" > "$DST"
else
  sed -e "s|\${LIVEKIT_API_KEY}|${LIVEKIT_API_KEY}|g" \
      -e "s|\${LIVEKIT_WEBHOOK_URL}|${LIVEKIT_WEBHOOK_URL}|g" "$SRC" > "$DST"
fi

exec /livekit-server --config "$DST" "$@"
