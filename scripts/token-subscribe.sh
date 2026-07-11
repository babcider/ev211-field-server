#!/usr/bin/env bash
# EV211 수신용 subscribe 토큰 발급 — 세대 룸 field-g<gen> 구독만 허용, 발행 불가 (짧은 TTL).
# 사용: ./token-subscribe.sh [GEN] [identity] [valid-for]
#   - GEN: 룸 세대(세션마다 +1). 기본 1 → 룸 field-g1.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

GEN="${1:-1}"
# identity 규약: listener-<uuid>. 미지정 시 uuid 자동 생성(중복 identity 로 인한 kick 방지).
gen_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z'; else
    printf '%s-%s' "$(date +%s)" "$RANDOM"; fi
}
IDENTITY="${2:-listener-$(gen_uuid)}"
VALID="${3:-10m}"   # 수신 토큰 TTL 10분 자동 갱신 정책 (D2')

ROOM="field-g${GEN}"

lk token create \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --room "$ROOM" \
  --identity "$IDENTITY" \
  --join \
  --grant '{"canPublish":false,"canSubscribe":true,"canPublishData":false}' \
  --valid-for "$VALID" \
  --token-only
