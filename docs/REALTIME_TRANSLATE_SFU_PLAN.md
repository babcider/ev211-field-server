<!-- ev211서버접속(모드 C)을 송신·수신·무전기 3모드로 확장하고, 송신에 gpt-realtime-translate AI통역을 넣어 클라우드 SFU로 브로드캐스트하는 아키텍처 계획 -->
# ev211서버접속(모드 C) 3-모드 확장 — gpt-realtime-translate × 클라우드 SFU

> 작성 2026-07-14 · 근거: `ev211-mobile/docs/COMPETITIVE_POSITIONING.md` §3, 기존 field-server SFU + ev211.com Edge 청취 파이프라인
> 스코프 확정(사용자): **모드 A(내부네트워크)·모드 B(폰서버)는 기능 그대로 둔다.** 신규 작업은 전부 **모드 C(ev211서버접속) 메뉴 안에서만** 진행한다.

---

## 0. 스코프

모드 C는 현재 **수신 전용**(6자리 코드 → Edge 청취)이다. 이를 모드 A와 같은 **송신·수신·무전기 3-모드**로 확장한다.

| 모드 C 하위 | 내용 | 전송 |
|---|---|---|
| **(1) 송신** | 원어 송출 + AI 통역(gpt-realtime-translate) | 클라우드 SFU publish |
| **(2) 수신** | ⓐ ev211앱 송출 수신 + ⓑ EdgePC 청취 수신(기존) | ⓐ SFU 구독 · ⓑ ActionCable chunk |
| **(3) 무전기** | 통역 없는 SFU 송수신(모드 A/B와 동일) | 클라우드 SFU duplex |

---

## 1. 핵심 통찰 — 모드 A의 LiveKit 경로를 클라우드로 재사용

모드 A(내부망)는 이미 **LiveKit SFU + field-api 토큰**으로 송신/수신/무전기를 구현해 두었다(`field_api.dart`·`send_screen.dart`·`receive_screen.dart`·`intercom_screen.dart`). 모드 C의 송신/수신(SFU)/무전기는 **이 코드 경로를 클라우드 서버로 향하게 하는 것**이 대부분이다. 프로토콜(LiveKit publish/subscribe, field-api 토큰)은 동일하다.

**모드 A와 다른 델타 3가지만 신규.**
1. 서버가 클라우드 → TOFU 자체서명(`tofu_http_client.dart`) 대신 **정식 TLS + TURN**(NAT 통과), 인증은 LAN 비번이 아니라 **ev211 계정/코드 기반**.
2. 진입이 IP 입력이 아니라 **6자리 코드**(기존 모드 C 패턴 재사용).
3. 송신에 **AI 통역 워커**가 붙는다(§4).

---

## 2. 서버 계층

```
                     ┌──────────────────────────────────────────┐
                     │  ev211.com (Rails) — 프론트 도어            │
                     │  · 세션/코드 레지스트리, 계정·인증          │
                     │  · manifest(transport 분기), 과금          │
                     │  · 기존 Edge chunk 파이프라인(ListenerCh)  │
                     └───────────────┬──────────────────────────┘
                                     │ 위임(SFU 세션·토큰)
                     ┌───────────────▼──────────────────────────┐
                     │  클라우드 field-server (FastAPI + LiveKit) │  ← revert 했던 field.ev211.com 부활
                     │  · SFU 룸·토큰·웹훅 (field-api 코드 재사용) │
                     │  · AI 번역 워커(gpt-realtime-translate)     │
                     │  · TURN(coturn), 정식 TLS                  │
                     └────────────────────────────────────────────┘
```

- **ev211.com(Rails) = 프론트 도어.** 6자리 코드 발급·해석, 계정/인증/과금, 그리고 **manifest에 `transport` 판별자**를 실어 준다: 기존 Edge 서비스 = `"chunk"`, 신규 앱 송출 = `"sfu"`(+SFU 룸·토큰·언어 채널 정보). 수신 클라이언트는 이 값으로 전송 경로를 고른다. → **점 (2)의 "두 소스 동시 수신"을 코드 하나로 해결.**
- **클라우드 field-server = 미디어·번역 계층.** 기존 field-api(토큰·룸·웹훅) 코드를 그대로 클라우드에 배포. LAN용 자체서명·무비번 정책 대신 클라우드용 TLS·TURN·토큰 인증만 교체.

> **결정 필요 A**: 서버 소유권 분할을 위 (ev211.com 프론트 + 클라우드 field-server 미디어)로 갈지, 아니면 ev211.com이 LiveKit을 직접 Kamal accessory로 물지(예전 revert안). 권장은 **field-server 재사용**(앱 `field_api.dart`가 이미 그 프로토콜을 말함 → 앱 신규 코드 최소).

---

## 3. (1) 송신 — 원어 + AI 통역 → SFU

```
송신자 앱 ──원어 mic──▶ 클라우드 SFU ch-00 (원어)
                            │ SFU 내부 구독
                            ▼
               [AI 번역 워커 · 목표 언어당 1개]  ──▶ ch-01(en) ch-02(zh) ...
```

- 송신 앱은 **원어 마이크를 ch-00으로 publish**하고, **목표 언어 목록을 선택**해 세션을 개설한다. 번역은 기기가 아니라 **서버 워커**가 수행.
- 세션 개설 시 ev211.com이 **6자리 청취 코드** 발급(기존 `services.listener_join_code` 체계 재사용). 송신 화면에 QR/코드 표시.
- 앱 UI: 모드 A `send_screen.dart` 재사용 + "AI 통역 언어 선택" 다중선택 추가. 원어만 송출(통역 0개)도 허용.
- **인증**: 송신은 ev211 계정 로그인 또는 송신 권한 코드. (결정 필요 B — §10)

### AI 번역 워커 (클라우드 field-server)
`gpt-realtime-translate`는 WebRTC 네이티브지만 워커는 **WebSocket 경로**로 붙인다(WebRTC↔SFU 직접 브리지는 재협상·트랜스코딩 복잡·테스트 곤란).

1. 워커가 LiveKit 룸에 publish 토큰(identity `xlate-<lang>`, publish `ch-NN`, subscribe `ch-00`)으로 join.
2. ch-00 원음 PCM 구독 → 24kHz mono PCM16 리샘플.
3. OpenAI WS `session.update`: pcm16 in/out, 목표 언어, **server VAD**, modality=audio(+선택 transcript).
4. 원음 → `input_audio_buffer.append`, 응답 `response.audio.delta` → LiveKit `AudioSource.capture_frame` → ch-NN publish.
5. 자막: `response.audio_transcript.delta` → LiveKit **data track**으로 발행(수신 앱이 원문+번역 렌더).

---

## 4. (2) 수신 — 앱 송출 + EdgePC 청취를 코드 하나로

수신은 **두 소스**를 모두 받는다. 6자리 코드 → ev211.com manifest의 `transport`로 분기.

- **`transport: "sfu"`(앱 송출)** — 클라우드 SFU 구독. 모드 A `receive_screen.dart` 경로 재사용(원어/통역 언어 채널 선택 + 자막 data track). 실시간 저지연.
- **`transport: "chunk"`(EdgePC 청취)** — 기존 `cloud_listen_api`·`cloud_cable`·`cloud_chunk_queue`·`cloud_listen_screen` 그대로. 교회 Edge 정밀 통역(seq·번역·audio_urls·출결·패스코드).

즉 **수신 진입 화면은 하나**, 코드 해석 결과에 따라 SFU 청취 화면 또는 기존 chunk 청취 화면으로 라우팅. 사용자는 소스를 의식하지 않는다.

> 기존 `cloud_screen.dart`의 코드 입력 → resolve → manifest 흐름을 유지하되, manifest에 `transport`가 오면 SFU 화면으로, 없으면(=chunk) 현재 `CloudListenScreen`으로 분기한다.

---

## 5. (3) 무전기 — SFU 송수신 (통역 없음)

- 모드 A `intercom_screen.dart`·`intercom_channels_screen.dart`·`intercom_logic.dart` 경로를 **클라우드 SFU로 재사용**. 통역 워커 없음.
- duplex PTT, 채널별 룸(`intercom-g<gen>-c<id>`), 8명 상호 소통 — 모드 A/B와 동일 계약.
- 클라우드 델타: TURN 경유 NAT 통과, 인증(계정/코드), TLS signaling.
- **1순위로 통역 없는 순수 SFU 송수신부터** 완성(사용자 지시). 통역 무전기는 후속.

---

## 6. 앱 코드 재사용 맵

| 모드 C 하위 | 재사용 | 신규 |
|---|---|---|
| 송신 | `send_screen.dart`·`send_setup_screen.dart`·`field_api.dart`·`mic_service.dart` | AI 통역 언어 선택 UI, 클라우드 인증, 코드 발급 표시 |
| 수신(SFU) | `receive_screen.dart`·`receive_logic.dart`·`field_api.dart` | manifest `transport` 분기 라우팅, 자막 data track 렌더 |
| 수신(chunk) | `cloud_listen_*`·`cloud_cable`·`cloud_chunk_queue` **전부 그대로** | — |
| 무전기 | `intercom_screen.dart`·`intercom_channels_screen.dart`·`intercom_logic.dart` | 클라우드 인증·TURN |
| 공통 | `doppler_theme`·`l10n`/`cloud_l10n` | 클라우드 서버 프로파일(TLS·TURN, non-TOFU) `server_store` 확장 |

`cloud_screen.dart`를 모드 A `connect_screen.dart`처럼 **송신/수신/무전기 3버튼 허브**로 개편한다.

---

## 7. 세션·비용·무중단 규칙 (반드시 준수)

- **OpenAI 세션 = 목표 언어당 1개.** 청취자당 세션 절대 금지 → 비용은 (언어 수 × 시간)에만 비례, 청취자 100명과 무관.
- **3시간+ 무중단**: OpenAI 세션 지속한계 → T-5분 make-before-break(둘째 WS 워밍 → 원자적 파이프 스위치 → 구세션 종료). LiveKit 트랙은 계속 publish 유지 → 청취 무중단. Edge 55분 갱신 패턴 재사용.
- **워커 크래시**: field-api 슈퍼바이저 백오프 재시작 → 트랙 republish → LiveKit 자동 재구독(empty_timeout 24h가 룸 생존 보장).

---

## 8. 모델 선택

- **1차 `gpt-realtime-translate`**(통합 STT+번역+TTS, WS 경로 republish). 스티칭(Whisper+LLM+TTS) 미채택. 부득이할 때만 TTS `gpt-4o-mini-tts`.
- **2차 Gemini 3.5 Live**: Preview·음성 일관성 리스크 → config 플래그 뒤 A/B만.

---

## 9. 단계별 실행

- [ ] **Phase A — 클라우드 SFU 부활 + 무전기 PoC**: 클라우드 field-server(LiveKit+field-api) 배포(TLS·TURN·토큰 인증). 모드 C 무전기(통역 없음) 먼저 — `intercom_*` 경로를 클라우드로. → **검증: 실기기 2대 클라우드 PTT 상호 소통, NAT 다른 망에서 TURN 통과.**
- [ ] **Phase B — 송신(원어) + 수신(SFU)**: 원어 송출 → SFU → 앱 수신, 6자리 코드 발급·해석, manifest `transport` 분기. → **검증: 앱 송출을 다른 앱이 코드로 수신, 첫 소리 지연 실측.**
- [ ] **Phase C — AI 통역 워커**: gpt-realtime-translate 워커, 언어별 ch-NN publish, 자막 data track. → **검증: 원어 송출 → en/zh 통역 채널 동시 청취, 지연·정확도 측정.**
- [ ] **Phase D — 수신 통합·무중단·다국어**: EdgePC chunk 수신과 앱 SFU 수신을 한 진입에서 라우팅, make-before-break 65분 연속, 3언어 동시. → **검증: chunk/sfu 코드 혼재 입장, 65분 무중단, 3언어.**

각 Phase 착수 시 `checklist.md`·`context-notes.md` 분리(전역 규칙 #7).

---

## 10. 결정 확정 (2026-07-14)

- **A. 서버 소유권 = 분할.** ev211.com(Rails) 프론트 도어(코드·계정·인증·과금·manifest·Edge chunk) + **클라우드 field-server(FastAPI+LiveKit) 미디어/번역 계층 재사용**. 앱 `field_api.dart`가 이미 field-api 프로토콜을 말하고, 8시간 스트레스 검증 코드를 그대로 씀. 두 서버 간 얇은 위임 API(세션 생성·토큰)만 신규.
- **B. 인증 = 계정에 귀속된 "송신 코드".** 관리자가 ev211.com 콘솔(계정/플랜)에서 송신 코드를 발급 → 현장 운영자가 앱에 코드 입력. Flutter OAuth 없이 과금은 계정에 정확히 귀속. 청취는 무인증 6자리 코드 유지. (순수 SSO=현장 마찰, 순수 권한코드=과금 귀속 불가 → 둘의 교집합)
- **C. TURN = LiveKit 내장 TURN(TLS 443) 먼저.** 별도 coturn 신설 없이 embedded TURN/TLS로 이종망·UDP 차단 폴백. 규모 확장 시에만 독립 coturn 분리.
- **D. 클라우드 SFU 도메인 = `field.ev211.com` 부활 확정.**

> 모드 A/B는 이 전체에서 **불변**. 오프라인 자산은 그대로 두고 모드 C만 온라인 확장한다(포지셔닝 §2-6 준수).

---

## 관련

- `ev211-mobile/docs/COMPETITIVE_POSITIONING.md` — 포지셔닝·기술 방향
- `docs/PHONE_SERVER_PLAN.md` — 폰서버(모드 B) SFU 패턴
- OpenAI realtime translation: https://developers.openai.com/api/docs/guides/realtime-translation
