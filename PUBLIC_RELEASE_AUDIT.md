# Public 공개 점검 결과

점검일은 2026-07-11입니다. 현재 커밋은 로컬 Ubuntu 필드 서버만 담은 새 Git 이력으로 구성됐으며, 코드와 문서 기준으로 public 공개가 가능합니다.

## 저장소 경계

- 포함 항목은 LiveKit, field-api, Caddy, 관리자·진단 웹, Docker Compose, Ubuntu 운영 도구입니다.
- EV211 클라우드, AI 번역, 결제, 데스크톱 앱, 모바일 앱 소스는 포함하지 않았습니다.
- Android·iOS 설명은 공개 앱 사용자를 위한 문서만 포함합니다.
- 실제 `.env`, 운영 백업, Caddy 개인키, 녹음 MP3, 로컬 작업 기록은 Git에서 제외됩니다.
- 기존 복합 저장소의 과거 이력을 가져오지 않고 검증된 현재 스냅샷으로 새 이력을 시작했습니다.

## 검증 결과

| 검사 | 결과 |
|---|---|
| `python -m pytest -q` | 142개 통과 |
| `openapi-spec-validator api/openapi.yaml` | 통과 |
| `pip-audit -r api/requirements.lock` | 알려진 취약점 0건 |
| Trivy 0.72.0 최종 field-api 이미지 검사 | 수정 가능한 HIGH·CRITICAL 취약점 0건 |
| Gitleaks 8.30.1 현재 파일·전체 Git 이력 검사 | 비밀정보 0건 |
| Docker Compose Linux host-network 병합 | LiveKit `network_mode: host`, 불필요한 `ports` 제거 확인 |
| `bash -n`·ShellCheck·관리자 JavaScript 문법 | 오류 0건 |
| field-api Docker 이미지 빌드·기동 import | 통과 |
| 격리 LiveKit 다중 트랙 서버 녹음 | 48kHz 모노 MP3 생성·`ffprobe` 확인 |

Docker 기본 이미지와 운영 이미지에는 태그와 multi-architecture digest를 함께 고정했습니다. Python 런타임 의존성은 해시가 포함된 `requirements.lock`으로 설치하며 GitHub Actions도 전체 커밋 SHA로 고정했습니다.

## 공개 후 운영자가 알아야 할 사항

- 이 서버는 신뢰할 수 있는 사설 LAN 전용이며 인터넷 공개와 포트 포워딩을 지원하지 않습니다.
- 녹음에는 참가자 음성과 개인정보가 들어가므로 운영자는 지역 법률에 맞는 고지·동의·보존 정책을 마련해야 합니다.
- App Store와 Google Play 링크는 앱 심사가 끝난 뒤 README와 앱 안내 문서에 추가해야 합니다.
- 실제 대상 Ubuntu 장비의 설치 검증은 해당 장비 접근 권한이 없어 수행하지 못했습니다. 격리 Docker 환경의 API·LiveKit·MP3 경로까지 검증했으며, 첫 현장 설치 후 `sudo ev211ctl doctor`와 실제 Android·iOS 단말 시험이 필요합니다.
- public 전환 후 GitHub의 비공개 취약점 신고와 Dependabot 보안 업데이트를 활성화하는 것을 권장합니다.
