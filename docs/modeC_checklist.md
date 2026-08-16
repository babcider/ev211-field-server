<!-- 모드 C(ev211서버접속) 3-모드 확장 개발 체크리스트 — REALTIME_TRANSLATE_SFU_PLAN.md 실행용 -->
# 모드 C 확장 체크리스트

계획: `docs/REALTIME_TRANSLATE_SFU_PLAN.md` · 결정 A/B/C/D 확정(§10).

## 증분 1 — AI 번역 워커 코어 (서버, 로컬 검증 가능) ✅ 완료(main 병합됨)
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

### 비용 가드 ✅ (2026-08-04 / 게이팅 2026-08-16)
- [x] AI 채널 idle 자동 종료 — 원음 프레임 10분 미수신 시 자동 close(`_ai_idle_sweep_loop`, 60초 주기). 기준 시각은 워커가 아니라 **채널 개설 시각**(워커 재시작에 리셋되지 않게).
- [x] **무음 구간 append 게이팅 — 구현·검증 완료(2026-08-16, `c75a26b`)**. `SilenceGate`(RMS 임계 150 / 유예 1.5초 / 선행 버퍼 0.4초, `config.py` 상수 + `AI_GATE_ENABLED` 회귀 스위치). 게이팅은 **OpenAI append 만** 멈추고 세션과 `last_floor_frame_at`(idle 가드 판정 근거)은 유지한다. `/ai-channels/{id}/status` 에 `gate` 지표 노출.
  - 실측(동일 시나리오 실키 E2E 대조 2회, 13.9초 2문장 + 전후 무음): 게이팅 off 송신 **31.21초** → on **9.35초** = **송신량 70.0% 절감**. 첫 번역 소리 7.3초 → 6.3초(악화 없음), 원문·번역 자막 전문 동일 = **발화 앞부분 잘림 없음**.
  - 단위 테스트 6종(무음 시 중단 / 선행 버퍼 포함 재개 / 세션 갱신 비간섭 / 비활성 스위치 / 원음 수신 기록 보존 / 순수 헬퍼).

### 자막 발화 경계 ✅ (2026-08-16)
- [x] **자막 `utterance_id` 추가 — 계약 §6c v1.7(`b016a12`)**. 앱의 "무음 4초" 휴리스틱을 대체할 서버측 발화 구분자. `seq` 는 전체 증가 유지, `utterance_id` 는 발화마다 증가하며 `kind` 별 독립 채번(번역 지연 때문에 공용 카운터를 쓰면 꼬리가 밀린다).
  - **실키 조사 결론**: gpt-realtime-translate 스트림에 **발화 완료 신호가 없다**. 관측 이벤트 5종(`session.created`·`session.updated`·`output_audio.delta`·`output_transcript.delta`·`input_transcript.delta`)뿐이고 `*.done`·`*.completed`·`speech_started|stopped` 는 오지 않는다 → 워커 자체 채번 규칙을 계약에 명시.
  - **채번 규칙**: 주 규칙 = 문장 종결 부호, 폴백 = 델타 간 8초 경과. 시간 간격을 주 규칙으로 쓸 수 없는 이유는 실측으로 확정 — 게이팅 ON 에서 번역 스트림의 발화 **안** 정체(2.61초)가 발화 **사이** 간격(2.23초)보다 **길다**(모델이 문장 꼬리 토큰을 붙들었다가 늦게 뱉는다). 앱의 구 4초 휴리스틱이 실패하던 원인이 이것이며 회귀 테스트로 잠갔다.
  - 실키 E2E 최종 실행에서 2문장 발화가 원문·번역 **모두 `utterance_id` 1·2 로 정확히 분리**됨을 로그로 확인. 하위호환 확인: ev211-mobile `CaptionEvent.tryParse` 는 알려진 키만 꺼내 쓰므로 기존 앱이 깨지지 않는다.
  - **앱 대응은 미완**(범위 밖) — `lib/live_captions.dart` 의 `CaptionBuffer` 는 여전히 `idleGap` 4초로 경계를 추정한다. `utterance_id` 를 읽도록 바꾸는 것이 다음 앱 작업이다.

## 증분 2 — 클라우드 배포 + 무전기 인증 (서버, 인프라 필요)
- [x] `docker-compose.cloud.yml` + LiveKit 내장 TURN — **AWS Lightsail 서울(15.165.188.147, $7 micro)에 배포됨(2026-08-15)**. TURN 은 UDP 3478 활성(인증서 불필요). **TURN-over-TLS(443)·Caddy `field.ev211.com` 은 DNS 전환+공인 인증서 후**(오버레이 주석 참조).
- [x] 클라우드 AI 통역 E2E — `ai_live_e2e.py` 공인망 2회 PASS(첫 소리 콜드 13.2초/웜 2.9초). 실기기(아이패드) 청취 PASS.
- [x] 무전기(통역 없음) 클라우드 PTT — **앱 수정 없이 모드 A 무전기 화면 + 클라우드 IP 로 동작**(2026-08-15 실기기 2대 상호 PTT, 서버 원장에 채널 1 동시 발행 기록). 송신·수신 화면도 동일하게 동작 확인. **이종망 검증 완료(2026-08-15)**: LTE 송신(223.38.227.71)→SFU→Wi-Fi 수신(122.42.175.35) 재생 확인. SFU 구조라 미디어는 항상 클라이언트↔서버 직결이며 이번 검증은 양 망 모두 UDP 도달 — TURN 은 UDP 차단 망 폴백으로 대기(강제 검증은 UDP 차단 환경 확보 시).
- [x] 계정 귀속 송신 인증 — **라이브 계약 v1.2 의 복합 Bearer(`join_code:send_password`) + verify_send 콜백으로 대체 구현·배포됨**. `SEND_CODE_DELEGATION.md` 의 8자리 코드 안은 채택되지 않았다(라이브별 송신 비번이 같은 목적을 더 단순하게 달성).
- [x] **사용량 보고(계약 §4) — field-server → Rails push 구현(2026-08-16)**. 참가자 세션 단위·60초 배치. Rails 수신측은 이미 있었고 송신측만 비어 있었다. 단위 20건 + 로컬 도커 E2E(송신/청취/ai_translate 실측 push) 통과. **클라우드 배포는 미완**.

## 증분 3 — 송신(원어)+수신(SFU)+코드 분기 (앱) ✅ 완료(2026-08-15~16, ev211-mobile)

리포는 `ev211-mobile`. 아래는 커밋 해시와 실제 코드를 확인한 결과다(추측 아님).

- [x] 3-버튼 허브 — `ad21d4c` "모드 C 를 라이브 허브 3진입점으로 개편". `cloud_screen.dart` 는 **삭제**되고 `lib/live_hub_screen.dart` 로 대체됐다. 진입점은 계획서의 (송신/수신/무전기)가 아니라 **개설 / 송신자 / 라이브 참여** 3갈래다(LIVE_PLAN §1 확정안). 무전기는 모드 A 화면을 그대로 쓴다(증분 2에서 앱 수정 없이 동작 확인).
- [x] 송신 원어 publish + AI 통역 언어 선택 — `3279e07`(AI 통역 채널 API·개설자 프로비저닝), `5d6b09b`(개설자 송신 진입 자동 프로비저닝·비번 재입력 제거), `9bddb12`(선택지를 카탈로그 13종으로 교체), `c8c3958`(인간 통역사 채널을 라이브 언어로 제한), `c2732ec`(개설 화면 원어 하드코딩 분리). 코드 확인: `live_create_screen.dart` 의 `kAiLanguageCatalog` 13종 + `aiLanguageChoices(source)` 가 원어를 제외하고, `live_created_screen.dart:79` 는 서버 값(`live.sourceLanguage?.code ?? 'ko'`)을 우선한다.
- [x] manifest `transport` 분기 라우팅(sfu/chunk) — `9cd016c` "라이브 참여(수신) 흐름 추가 — transport 분기". 코드 확인: `live_api.dart` 가 `transport` 를 파싱해 `isSfu` 를 노출하고, `live_join_screen.dart:82` 가 `resolved.isSfu` 로 sfu(ChannelListScreen)와 chunk(CloudListenScreen) 경로를 가른다.
- [x] (계획 밖 추가분) `76ef22e` sfu 수신 화면 AI 자막 오버레이(계약 §6c), `bfa3eac`·`c716fa4`·`72f3104` 인앱 QR 스캐너·유니버설 링크, `5fb0126` 복합 Bearer.

## 증분 4 — 통합·무중단·다국어 (진행 중)
- [ ] **chunk/sfu 수신 통합** — 미완. 현재는 `live_join_screen.dart` 가 transport 로 **분기**만 하고(증분 3 완료분), 두 경로는 각각 `CloudListenScreen`(chunk)·`ChannelListScreen`(sfu) 이라는 별개 화면이다. 단일 수신 화면으로 합치는 작업이 남았다.
- [ ] **make-before-break 55분 실경과 검증** — 미검증. 단위 테스트(원자 스위치·드레인·갱신 실패 시 구세션 유지)와 20~30초 E2E 로만 확인됐고, **실제 55분을 넘겨 교체되는 장면은 관측된 적이 없다**. 65분 연속 송출 장시간 검증(T6)으로 분리해 별도 진행한다. 관측 항목: 교체 전후 오디오 공백, `renewals` 증가, `restarts` 0 유지, 자막 seq 연속성.
- [ ] **3언어 동시** — 미검증. 워커·슈퍼바이저는 언어당 세션 1개 구조로 설계돼 있으나(비용도 언어 수에 비례), 3언어를 동시에 띄운 실측 기록이 없다.

> 인프라(field.ev211.com DNS/호스트/TLS/TURN) 프로비저닝 = 사용자 ops 단계.
