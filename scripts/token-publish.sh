#!/usr/bin/env bash
# EV211 송신용 publish 토큰 발급 — 세대 룸 field-g<gen>, 마이크 발행 허용 + duplex 수신 허용.
# 사용: ./token-publish.sh <ch-00..ch-15> [EPOCH] [GEN] [valid-for] [NONCE]
#   - EPOCH: 채널 세대(takeover 마다 +1). 기본 1.
#   - GEN:   룸 세대(비번 회전·세션마다 +1). 기본 1 → 룸 field-g1.
#   - NONCE: lease 식별자 재사용 방지용 짧은 랜덤 문자열(codex 5차). 생략 시 자동 생성.
#            TTL 만료 후 같은 EPOCH·GEN 재발급 시 identity 재사용을 막아, 이전 연결의
#            지연된 participant_left 가 새 lease 를 잘못 해제하는 것을 방지한다.
#            실서비스에서는 field-api 가 lease 발급 시 생성하며, 이 스크립트는 스파이크 검증용.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

CHANNEL="${1:-ch-00}"
EPOCH="${2:-1}"
GEN="${3:-1}"
VALID="${4:-1h}"   # 송신 토큰 TTL 1시간 (D2')
# NONCE 미지정 시 6자리 랜덤 hex 자동 생성(서버가 lease 발급 시 만드는 값의 스파이크 대체).
NONCE="${5:-$(openssl rand -hex 3)}"

ROOM="field-g${GEN}"
IDENTITY="speaker-${CHANNEL}-e${EPOCH}-g${GEN}-n${NONCE}"

# canPublishSources=microphone(--allow-source) 로 마이크만 발행 허용.
# canSubscribe:true 로 duplex(송신자가 Floor/다른 채널 동시 청취)를 지원한다.
# 어느 채널 트랙을 발행하는지(=CHANNEL) 강제와 중복 송신 거부는 서버 웹훅(field-api, Phase 1)의 몫이다.
# 이 토큰은 채널 스코프를 grant 로 고정하지 않으며, identity·metadata 의 채널 정보를 웹훅이 검증한다.
lk token create \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --room "$ROOM" \
  --identity "$IDENTITY" \
  --join \
  --allow-source microphone \
  --grant '{"canPublish":true,"canSubscribe":true,"canPublishData":false}' \
  --metadata "{\"channel\":\"$CHANNEL\",\"epoch\":$EPOCH,\"gen\":$GEN,\"nonce\":\"$NONCE\"}" \
  --valid-for "$VALID" \
  --token-only
