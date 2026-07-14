<!-- 모드 C 확장 개발 중 내린 결정과 근거 — 세션 간 이어받기용 컨텍스트 노트 -->
# 모드 C 컨텍스트 노트

## 확정 결정 (2026-07-14)
- **A. 서버 분할**: ev211.com(Rails) 프론트 도어 + 클라우드 field-server(FastAPI+LiveKit) 미디어/번역. 앱 `field_api.dart`가 이미 field-api 프로토콜을 말해 앱 신규 코드 최소. 8시간 스트레스 검증 코드 재사용.
- **B. 계정 귀속 송신 코드**: 관리자가 ev211.com 콘솔에서 발급 → 앱 코드 입력. Flutter OAuth 회피 + 과금 계정 귀속. 청취는 무인증 6자리 코드 유지.
- **C. LiveKit 내장 TURN(TLS 443)**: 별도 coturn 신설 없이 시작. 규모 확장 시 분리.
- **D. `field.ev211.com` 부활 확정**.

## 개발 순서 근거
- 무전기(통역 없음)가 제품 롤아웃 1순위이나 **검증에 라이브 클라우드 SFU 필요**(인프라=사용자 ops).
- AI 번역 워커는 **로컬 단위 테스트로 완결 검증** 가능 + 기술 최대 불확실성 → **먼저 만들어 리스크 제거**(개발 순서 ≠ 롤아웃 순서).
- 스토어 앱(ev211-mobile)은 미변경 유지. 증분 1은 field-server 리포 한정(CI 있음), 브랜치 작업·main 미푸시(사용자 리뷰 후).

## 기술 사실 (정찰 2026-07-14)
- codex-cli 0.144.1. field-server main 클린.
- `livekit==1.1.13`(rtc 풀 SDK, AudioSource publish 가능), `livekit-api==1.1.1`.
- 기존 테스트 계약: **LiveKit API는 mock**(`livekit_client.py`를 mock). 워커 테스트도 OpenAI·LiveKit mock.
- ch-00 = Floor(원음), publish 토큰은 `can_publish_sources=["microphone"]` + duplex subscribe. 청취는 무인증 subscribe 토큰(TTL 10분).
- DB 스키마: `db.py`의 `CREATE TABLE IF NOT EXISTS` 패턴. 기존 마이그레이션 수정 금지 — 신규 컬럼은 방어적 ADD.

## 실 키 E2E 확인 (2026-07-15)
- **gpt-realtime-translate 프로토콜 실측 확인 완료**(키: `~/CursorProjects/ev211/.env` 의 `OPENAI_API_KEY`, field-server `.env` 아님). 영어 음성(macOS `say`+ffmpeg 24kHz PCM16)→ 한국어 실번역 성공.
- **확인된 계약**(워커 가정과 전부 일치): 엔드포인트 `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate`, client→server 이벤트는 **`session.` 프리픽스 필수**(`session.update`·`session.input_audio_buffer.append`·`session.close` — 프리픽스 빠지면 invalid_value 거부, 첫 프로브가 이걸로 실패), server→client `session.created`/`session.updated`(type=translation, `expires_at` 존재)·`session.output_audio.delta`(payload sample_rate=24000·channels=1·format=pcm16, 오디오는 `delta` base64)·`session.output_transcript.delta`·`session.input_transcript.delta`. 워커 recv 핸들러·AudioFrame(24kHz mono) 정확.
- **잠금 테스트**: `api/tests/test_openai_e2e.py`(RUN_OPENAI_E2E=1 + OPENAI_API_KEY 게이트, CI 기본 skip). 실행: `RUN_OPENAI_E2E=1 OPENAI_API_KEY=... pytest api/tests/test_openai_e2e.py`.
- **미확인 잔여**(라이브 LiveKit 필요): VAD/turn_detection 실동작, 전체 파이프라인(Floor 구독→republish), make-before-break 실검증. `session.created.expires_at` 로 갱신 타이밍 잡을 수 있음(현재 미구현).

## 열린 항목
- ev211.com Rails 측 송신 코드 레지스트리·manifest `transport` 필드 = 별도 리포(ev211) 작업, 증분 2~3에서.
- 라이브 LiveKit 포함 전체 파이프라인 E2E 는 클라우드 인프라 준비 후.
