<!-- 모드 C(ev211서버접속) 3-모드 확장 개발 체크리스트 — REALTIME_TRANSLATE_SFU_PLAN.md 실행용 -->
# 모드 C 확장 체크리스트

계획: `docs/REALTIME_TRANSLATE_SFU_PLAN.md` · 결정 A/B/C/D 확정(§10).

## 증분 1 — AI 번역 워커 코어 (서버, 로컬 검증 가능) ✅ 완료(브랜치, main 미푸시)
- [x] OpenAI gpt-realtime-translate WS 계약 조사(번역 전용 `session.*` 스키마·24kHz PCM16·60분 한계)
- [x] LiveKit Python 1.1.13 서브스크라이브(원음 PCM)·AudioSource publish 패턴 조사(recording.py 참조)
- [x] `translate_worker.py` — ch-00 구독 → OpenAI WS → ch-NN republish, 세션 갱신 훅(구조)
- [x] `db.py` 마이그레이션 — channels.source/target_language 방어적 ADD(멱등·동시기동 안전)
- [x] field-api 엔드포인트 — `POST/DELETE/GET /ai-channels` + 워커 슈퍼바이저(백오프·leaked 수거)
- [x] pytest(OpenAI·LiveKit mock) 그린 — **212 passed**
- [x] codex 검수 **6회전 수렴**(11→회귀4→미흡1→심층4→3→0, "남은 실제 결함 없음") → 커밋 3개(f33eb60·68213ef·0d1d4ce)

### 증분 1 후속 ✅ 완료(2026-08-03)
- [x] make-before-break 오디오 무중단 연속성(둘째 WS 워밍→원자 스위치→8초 드레인, 실패 시 구세션 유지·30초 재시도)
- [x] 자막 data track 발행(topic `captions`, `issue_publish_token(can_publish_data=True)` 는 AI 워커 토큰만)
- [x] 서버 재시작 시 AI 채널 워커 자동 복원(`AppState.restore_ai_channels`, 세대 변경·키 부재는 기존대로 close)
- [x] `openapi.yaml`에 `/ai-channels` 3종 계약 반영(tag `ai` + 스키마 3종, validator OK)
- [x] **실 키 E2E로 gpt-realtime-translate 프로토콜 확인 완료** — 영어→한국어 실번역 성공("오늘 예배에 오신 걸 환영합니다"), 이벤트명(`session.output_audio.delta`·`session.output_transcript.delta`·`session.input_transcript.delta`)·세션 스키마(type=translation)·오디오 포맷(24kHz PCM16 mono)·`session.*` 프리픽스 전부 워커 가정과 일치. 계약 잠금 테스트 `test_openai_e2e.py`(RUN_OPENAI_E2E 게이트, CI 기본 skip).
- [x] **라이브 LiveKit 포함 전체 파이프라인 E2E 통과(2026-08-03)** — 로컬 도커 스택(livekit+field-api)에서 원음 ch-00 publish→워커 구독→번역→ch-01 republish→청취까지 실동작. 하니스 `scripts/ai_live_e2e.py`, 첫 소리 지연 3.1~3.4초, 자막 101패킷, 번역 역전사로 한국어 확인. 클라우드 인프라 불필요했음.

### 비용 가드 ✅ (2026-08-04)
- [x] AI 채널 idle 자동 종료 — 원음 프레임 10분 미수신 시 자동 close(`_ai_idle_sweep_loop`, 60초 주기). 기준 시각은 워커가 아니라 **채널 개설 시각**(워커 재시작에 리셋되지 않게).
- [ ] 무음 구간 append 게이팅(발행 중이지만 아무도 말하지 않는 시간의 토큰 절감) — 실사용 패턴 확인 후 판단.

## 증분 2 — 클라우드 배포 + 무전기 인증 (서버, 인프라 필요)
- [ ] `docker-compose.cloud.yml` + LiveKit 내장 TURN(TLS 443) + Caddy `field.ev211.com`
- [ ] 계정 귀속 송신 코드 발급/검증 → publish 토큰 (ev211.com 위임 계약)
- [ ] 무전기(통역 없음) 클라우드 PTT — `intercom_*` 경로 클라우드 대상 (앱)

## 증분 3 — 송신(원어)+수신(SFU)+코드 분기 (앱)
- [ ] `cloud_screen.dart` 3-버튼 허브(송신/수신/무전기)
- [ ] 송신 원어 publish + AI 통역 언어 선택
- [ ] manifest `transport` 분기 라우팅(sfu/chunk)

## 증분 4 — 통합·무중단·다국어
- [ ] chunk/sfu 수신 통합, make-before-break 65분, 3언어 동시

> 인프라(field.ev211.com DNS/호스트/TLS/TURN) 프로비저닝 = 사용자 ops 단계.
