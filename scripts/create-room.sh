#!/usr/bin/env bash
# EV211 룸 생성 — auto_create=false 이므로 스파이크 테스트 전 세대 룸을 명시적으로 생성한다.
# 프로덕션에서는 field-api(Phase 1)가 룸을 생성/관리한다. 이 스크립트는 스파이크 전용.
# 사용: ./scripts/create-room.sh [GEN|room-name]
#   - 인자가 숫자면 세대 룸 field-g<GEN> 을 생성(기본 1 → field-g1).
#   - 인자가 그 외 문자열이면 그 이름을 룸명으로 그대로 사용.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

ARG="${1:-1}"
if [[ "$ARG" =~ ^[0-9]+$ ]]; then ROOM="field-g${ARG}"; else ROOM="$ARG"; fi

echo "== 룸 생성: $ROOM (empty_timeout=300s) =="
lk room create \
  --url "${LIVEKIT_WS_URL:-ws://localhost:7880}" \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --empty-timeout 300 \
  "$ROOM"
