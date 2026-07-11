#!/usr/bin/env bash
# EV211 오디오 부하 테스트 실행 — lk load-test 로 audio publisher/subscriber 시뮬레이션
# 사용: ./loadtest.sh <audio-publishers> <subscribers> <duration>
# 예:   ./loadtest.sh 15 200 60s
#
# [부하 모델 한계 — 정직한 명시]
# 제품 모델은 "각 subscriber 가 트랙 1개만 구독"(단일 채널 청취)이다.
# 그러나 lk load-test 는 subscriber 가 룸의 "모든" 발행 트랙을 구독하며,
# 트랙 수를 subscriber 별로 제한하는 옵션이 없다(lk 2.17 기준: --room/--audio-publishers/
# --subscribers/--num-per-second/--layout 만 존재, per-sub 트랙 제한 옵션 없음).
# 따라서 pub=15 이면 각 sub 가 15트랙을 구독 → 제품보다 15배 무거운 최악 부하 모델이다.
# 제품 모델(1트랙/sub) 검증은 lk load-test 로는 불가하며, Phase 1 field-api + 실제 클라이언트
# 다중 인스턴스 또는 커스텀 부하 클라이언트로 별도 측정해야 한다.
# 제품 모델 근사가 필요하면 pub=1 로 실행(각 sub 가 1트랙만 구독)해 하한을 본다.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

PUBS="${1:-15}"
SUBS="${2:-200}"
DUR="${3:-60s}"

# --- ulimit(open file descriptor) 점검 ---
# subscriber 다수 = 소켓/fd 다수. 기본 256(macOS)이면 수백 sub 에서 "too many open files" 로
# 조기 실패해 서버 상한이 아니라 클라이언트 fd 상한을 측정하게 된다. 넉넉히 올린다.
CUR_NOFILE="$(ulimit -n)"
NEED_NOFILE=$(( (PUBS + SUBS) * 16 + 1024 ))
echo "== fd 점검: 현재 ulimit -n=$CUR_NOFILE, 권장 >= $NEED_NOFILE =="
if [ "$CUR_NOFILE" != "unlimited" ] && [ "$CUR_NOFILE" -lt "$NEED_NOFILE" ]; then
  if ulimit -n "$NEED_NOFILE" 2>/dev/null; then
    echo "   → ulimit -n 을 $NEED_NOFILE 로 상향(이 셸 한정)."
  else
    echo "   ⚠ ulimit 상향 실패. 결과가 fd 상한에 걸릴 수 있음. 'ulimit -n $NEED_NOFILE' 후 재실행 권장."
  fi
fi

echo "== load-test: audio-publishers=$PUBS subscribers=$SUBS duration=$DUR =="
echo "== 주의: 각 subscriber 는 $PUBS 개 트랙 전체를 구독한다(제품 모델=1트랙과 다름, 상단 주석 참고). =="
lk load-test \
  --url "${LIVEKIT_WS_URL:-ws://localhost:7880}" \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --room field \
  --audio-publishers "$PUBS" \
  --subscribers "$SUBS" \
  --duration "$DUR"
