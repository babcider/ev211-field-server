#!/usr/bin/env bash
# 기존 deploy.sh 사용자를 ev211ctl의 백업 포함 안전 업데이트 명령으로 연결한다
set -euo pipefail

if command -v ev211ctl >/dev/null 2>&1; then
  exec ev211ctl update
fi

echo "ev211ctl이 설치되지 않았습니다. 먼저 sudo ./scripts/install.sh를 실행하세요." >&2
exit 1
