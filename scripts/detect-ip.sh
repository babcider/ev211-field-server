#!/usr/bin/env bash
# EV211 서버 LAN IP 자동 감지 — .env 의 FIELD_NODE_IP 에 넣을 값을 출력한다.
# 사용: ./scripts/detect-ip.sh          (감지된 IP 출력)
#       ./scripts/detect-ip.sh --write  (감지된 IP 로 .env 의 FIELD_NODE_IP 갱신)
set -euo pipefail
cd "$(dirname "$0")/.."

detect() {
  case "$(uname -s)" in
    Darwin)
      # 기본 라우트가 나가는 인터페이스의 IPv4
      local iface
      iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
      if [ -n "${iface:-}" ]; then
        ipconfig getifaddr "$iface" 2>/dev/null && return 0
      fi
      # 폴백: en0 → en1
      ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null
      ;;
    Linux)
      # 기본 라우트 소스 주소
      ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
      ;;
    *)
      echo "지원하지 않는 OS: $(uname -s)" >&2; return 1
      ;;
  esac
}

IP="$(detect || true)"
if [ -z "${IP:-}" ]; then
  echo "LAN IP 감지 실패. 수동으로 .env 의 FIELD_NODE_IP 를 설정하세요." >&2
  exit 1
fi

if [ "${1:-}" = "--write" ]; then
  # 모바일 앱 signaling 기본값: 평문 ws://<IP>:7880 직결(#3).
  # LiveKit SDK 는 자체서명 wss 의 TOFU 예외를 공유하지 않아 CA 미설치 단말이
  # wss://IP:8443 에 연결하지 못한다. signaling 에는 비밀번호가 실리지 않고 단기 JWT 만
  # 노출되며 미디어는 WebRTC DTLS-SRTP 로 항상 암호화된다.
  WS_URL="ws://${IP}:7880"
  # FIELD_NODE_IP 갱신(없으면 추가).
  if grep -q '^FIELD_NODE_IP=' .env 2>/dev/null; then
    awk -v ip="$IP" '/^FIELD_NODE_IP=/{print "FIELD_NODE_IP=" ip; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf 'FIELD_NODE_IP=%s\n' "$IP" >> .env
  fi
  # FIELD_WS_URL 도 감지 IP 기반 평문 ws 로 갱신(없으면 추가). 루프백 기본값 잔존 방지(§결함 9).
  if grep -q '^FIELD_WS_URL=' .env 2>/dev/null; then
    awk -v u="$WS_URL" '/^FIELD_WS_URL=/{print "FIELD_WS_URL=" u; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf 'FIELD_WS_URL=%s\n' "$WS_URL" >> .env
  fi
  echo ".env 갱신: FIELD_NODE_IP=$IP, FIELD_WS_URL=$WS_URL"
else
  echo "$IP"
fi
