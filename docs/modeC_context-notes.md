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

## 라이브 파이프라인 E2E 확인 (2026-08-03)
- **클라우드 인프라 없이 로컬 도커 스택으로 전 구간 검증 완료.** 원음(ch-00) publish → 워커 구독 → gpt-realtime-translate → ch-01 republish → 청취자 수신까지 실제로 흘렀다. 하니스 = `scripts/ai_live_e2e.py`(레포 커밋).
- **실측치**: 영어 12초 발화 → 첫 한국어 소리까지 **3.1~3.4초**, 오디오 프레임 83건·자막 패킷 101건·restart 0. 번역 결과를 whisper-1 로 역전사해 실제 한국어임을 확인("여러분 안녕하세요, 오늘 예배에 오신 걸 환영합니다…").
- **함정 1 — field-api 는 Caddy `/api` 프리픽스 뒤에 있다.** 로컬 접근은 `http://localhost:8880/api/...`(Host=localhost 인 루프백 블록만 해당, `127.0.0.1:8880` 은 사이트 블록에 안 걸림). 프리픽스 없이 치면 정적 파일 서버가 빈 200 을 돌려줘 오해하기 쉽다.
- **함정 2 — `docker-compose.yml` 이 `OPENAI_API_KEY` 를 field-api 에 전달하지 않았다.** 키가 있어도 컨테이너 안에서는 빈 값이라 `/ai-channels` 가 항상 503. 배선 추가 + `.env.example` 문서화 완료.
- 로컬 실행: `docker compose --env-file .env.local up -d`(`.env.local` = gitignore 대상, LiveKit 키·비번·OPENAI_API_KEY). 하니스용 venv 는 `.venv`(livekit 1.1.13).
- 테스트 음성은 macOS `say -v Samantha` → `afconvert -f WAVE -d LEI16@48000 -c 1`(ffmpeg 불필요).

## 후속 4건 구현 (2026-08-03)
- **자막 data track**: 워커가 `session.*_transcript.delta` 를 LiveKit data 패킷(topic `captions`)으로 발행. 페이로드 `{channel_id, track_name, kind(source|target), language, seq, delta}`. **`issue_publish_token(can_publish_data=…)` 인자를 새로 뒀다** — 사람 송신자는 기본 False 유지(자막 위조 방지), AI 워커 토큰만 True. 발행 실패는 오디오를 죽이지 않고 최초 1회만 warning.
- **make-before-break**: 수신 구조를 '세션별 reader → 공용 큐 → 단일 소비 루프' 로 바꿨다. 55분에 둘째 WS 를 먼저 connect(워밍) → `self._session` 원자 교체(send 루프가 다음 프레임부터 새 세션 사용) → 구세션은 8초 드레인 후 close. **워밍 실패는 구세션 유지 + 30초 후 재시도**(끊지 않는다). 구세션의 오류·종료는 무시하고 잔여 오디오만 계속 발행 — 그래야 교체가 재시작으로 번지지 않는다.
- **재시작 복원**: `AppState.restore_ai_channels()` 를 bootstrap 말미(reconcile 이후)에 호출. 순서가 중요하다 — 고아 lease 정리 전에 복원하면 새 워커 identity 와 어긋난다. **세대 변경·OPENAI_API_KEY 부재는 복원 불가**라 기존대로 닫는다. 복원 실패 채널은 슬롯 반납(close). `main._ai_token_provider` 는 `AppState.ai_token_provider` 로 옮겨 개설·복원이 같은 프로바이더를 쓴다.
- **실서버 확인**: AI 채널 개설 → `docker restart field-api` → 채널 state=open 유지·워커 running=true·새 세션으로 복원됨.
- `openapi.yaml` 에 `/ai-channels` 3종 + `AiChannelCreate`·`AiChannel`·`AiChannelWorker` 스키마 + tag `ai` 반영(openapi-spec-validator OK).

## 비용 가드 — idle 자동 종료 (2026-08-04)
- **동기**: 워커는 무음까지 OpenAI 로 연속 append 하므로(서버 VAD 계약) 과금이 **발화량이 아니라 송신이 켜져 있는 시간 × 언어 수**에 비례한다. 송신자가 사라졌는데 채널이 열려 있으면 토큰이 계속 나간다.
- **판정값**: `AiTranslateChannel.floor_idle_seconds()` — 원음 프레임 미수신 경과. `_ai_idle_sweep_loop` 이 60초마다 훑어 `AI_IDLE_CLOSE_SECONDS`(10분) 초과 채널을 DELETE 와 같은 절차로 닫는다(stop → participant remove → close → cascade).
- **기준 시각은 채널 개설 시각이지 워커 기동 시각이 아니다.** 워커 `started_at` 은 크래시 재시작마다 초기화되므로, 그걸 기준으로 삼으면 재시작을 반복하는 채널이 idle 판정을 영원히 회피한다. 최초 구현이 워커 기준이었고 라이브 확인 중에 잡아 채널(슈퍼바이저) 기준으로 옮겼다.
- **발행자 없음 vs 무음은 다르다**: 발행자가 붙어 있으면 무음이라도 프레임이 계속 오므로 idle 이 아니다(정상 — 예배 중 침묵을 끊으면 안 되니까). 이 가드가 막는 것은 "송신 종료·미개시인데 채널만 열린" 상태다. 무음 구간 append 게이팅은 별도 과제(실사용 패턴 확인 후).
- 상수: `AI_IDLE_CLOSE_SECONDS=600`, `AI_IDLE_CHECK_SECONDS=60`.

## 사용량 보고 구현 (2026-08-16, 계약 §4 v1.6)
- **동기**: Rails 수신측(`POST /api/field/usage` + `FieldUsageRecord` + 슈퍼어드민 사용 모니터링 화면)은 완비돼 있었는데 field-server 송신측이 없어 화면이 비어 있었다. 계약에 정의만 되고 미구현으로 남아 있던 마지막 서버 항목.
- **파생 지점을 하나로**: 세션 훅을 여러 라우트에 심지 않고 `AppState.record_signal_event` **한 곳**에 걸었다. webhook 과 main 이 모두 이 통로로 참가/이탈을 기록하므로, 여기 하나만 잡으면 send·listen·intercom·ai_translate 가 전부 덮인다(변경 표면 최소화 = 회귀 위험 최소화).
- **kind 는 참가 좌표로 판정한다**: 릴레이 룸 송신자 → 채널이 `source='ai'` 면 `ai_translate`(language=target_language), 아니면 `send`. 릴레이 룸 수신자 → `listen`. 인터컴 룸 → `intercom`. AI 워커도 speaker identity 로 참가하므로 별도 훅 없이 자동으로 잡힌다.
- **청취자 채널은 원장 역참조로 얻는다**: 청취자 `participant_joined` 웹훅에는 channel_id 가 없다(webhook.py 가 receive 는 채널을 채우지 않는다). 직전 `token_issued`(구독 토큰 발급) 이벤트에 채널이 있으므로 `last_event_channel_for_subject` 로 되짚는다 — webhook.py 를 건드리지 않으려는 선택.
- **join_code 귀속**: 룸 하나 = 라이브 하나라, 송신 인증(복합 Bearer)에 성공한 마지막 join_code 를 활성 라이브로 두고(`settings.active_join_code` 로 영속) 세션 개설 시 스냅샷한다. 채널마다 컬럼을 두는 대신 이 방식을 택한 이유는 청취·무전기처럼 인증을 거치지 않는 참가자도 같은 라이브로 귀속돼야 하기 때문. 이미 열린 세션은 라이브가 바뀌어도 귀속이 변하지 않는다.
- **마감 경로 3중**: ① 이탈 웹훅 ② `close_channel`(수동 종료·idle 가드·세대 회전 — 이탈 웹훅을 기다리지 않는다) ③ 6시간 좀비 스윕. ③은 **started_at + 상한**으로 마감한다(now 로 마감하면 재시작 때마다 과대 청구).
- **집계 실패가 통역을 막지 않는다**: `_track_usage` 는 예외를 삼킨다. 사용량은 부가 기능이고 오디오 경로를 죽이면 안 된다.
- **검증**: 단위 20건(뮤테이션 2종으로 테스트가 실제로 잡는지 확인) + 로컬 도커 E2E. 송신 15초·청취 12초·ai_translate 16초가 실제 LiveKit 참가/이탈에서 나와 배치로 push 됐고, 페이로드가 Rails 컨트롤러 테스트 형식과 일치했다(양쪽에 zh-CN 케이스 회귀 테스트 추가).
- **하니스**: 가짜 ev211 수신 서버(scratchpad, 세션 휘발) + `usage_e2e.py`. AI 워커 없이 송신·청취만으로도 세션 파생을 검증할 수 있어 OpenAI 토큰 0 으로 회귀 확인이 가능하다 — 패턴만 기억하고 필요 시 재작성.
- **미배포**: 코드·문서·테스트는 main 에 있으나 클라우드 field-server 에는 아직 올리지 않았다(다음 배포 때 rsync + `--build` 필수 — 구버전 이미지 배포 함정 재발 주의).

## 열린 항목
- ev211.com Rails 측 송신 코드 레지스트리·manifest `transport` 필드 = 별도 리포(ev211) 작업, 증분 2~3에서.
- make-before-break 는 단위 테스트 + 로컬 파이프라인까지 검증했고, **55분 실경과 교체는 미검증**(장시간 런 필요).
- 클라우드 배포(증분 2)는 호스트 결정 대기 — `field.ev211.com` DNS 는 현재 ev211.com(1.234.63.213)을 가리키고 TLS 미발급. 그 호스트는 Kamal 프록시가 80/443 을 점유하므로 LiveKit 내장 TURN(TLS 443)과 충돌한다(결정 C 재검토 또는 별도 인스턴스 필요).
