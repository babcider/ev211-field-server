<!-- 라이브(모드 C) 웹·앱·field-server 3자 공유 API 계약 — 병렬 개발의 단일 기준. LIVE_PLAN.md 의 실행 계약 -->
# 라이브 API 계약 v1 — 2026-08-15

웹(Rails)·앱(Flutter)·field-server 가 병렬 개발 시 공유하는 계약. 변경은 이 문서를 먼저 고치고 각 구현에 반영한다.

## 0. 공통 규칙

- **접속코드(join_code)**: 6자, 문자셋 `23456789ABCDEFGHJKMNPQRSTUVWXYZ`(31자 — 0/O·1/I/L 혼동 문자 제외). 서버 저장은 대문자, 입력은 대소문자 무관(서버·앱 모두 upcase 후 비교). 전역 unique.
- **송신 비번(send_password)**: 숫자 6자리 zero-pad 문자열. 라이브별 자동 생성.
- **연동 코드(link_code)**: 8자, join_code 와 같은 문자셋. TTL 10분.
- 서버 간 콜백 인증: 헤더 `X-Field-Secret`(공유 시크릿, 양측 .env `FIELD_CALLBACK_SECRET`).
- 비밀값(비번·코드)은 **URL 쿼리에 싣지 않는다** — 반드시 POST body.

## 1. 접속코드 resolve — 앱/웹 수신 진입점 (무인증)

`GET /listen/codes/:code` (기존 라우트 확장 — 제약 `\d{6}` → `[A-Za-z0-9]{6}`, 컨트롤러에서 upcase)

200:
```json
{
  "stream_key": "uuid",                      // 기존 유지(하위 호환)
  "listen_path": "/listen/<stream_key>",
  "manifest_path": "/listen/<stream_key>/manifest.json",
  "live": {
    "title": "주일 라이브",
    "transport": "chunk",                    // "chunk"(Edge 정밀) | "sfu"(클라우드 실시간)
    "field_server_url": null,                 // sfu 일 때 "https://<host>:8443/api"
    "field_ws_url": null,                     // sfu 일 때 "ws://<host>:7880"
    "source_language": {"code":"ko","label":"한국어"},
    "languages": [{"code":"en","label":"영어"}]
  }
}
```
404: `{ "error": "유효하지 않은 코드입니다." }`
- `transport` = `services.live_transport`(신규 컬럼, 기본 "chunk"). 앱 개설 라이브는 "sfu".
- field 서버 주소는 **슈퍼어드민 설정값**(신규 설정 모델)에서 읽는다. 앱 하드코딩 금지.

## 2. Service 확장 (Rails 마이그레이션)

- `listener_join_code`: 신형식 재발급(기존 숫자 코드 전부 신형식으로 교체 — 데이터 마이그레이션)
- `live_send_password` (string, 숫자 6자리): listener_enabled 활성 시 join_code 와 함께 발급
- `live_transport` (string, default "chunk", null: false)
- `live_intercom_enabled` (boolean, default false) — §6 요청 필드 저장(과금·모니터링 근거). [v1.1 추가]

## 3. 송신 비번 검증 콜백 — field-server → Rails

`POST /api/field/verify_send`  헤더 `X-Field-Secret`
```json
{"join_code": "K3M7PQ", "send_password": "483920"}
```
- 200 `{"ok": true, "service_id": 12, "church_id": 3, "title": "..."}`
- 403 `{"ok": false, "reason": "invalid_password"}` / 404 `{"ok": false, "reason": "not_found"}`(코드 없음·비활성) / 401 시크릿 불일치 [v1.1: 404 바디 명시]

## 4. 사용량 수집 — field-server → Rails (모니터링 데이터원)

`POST /api/field/usage`  헤더 `X-Field-Secret`
```json
{"events": [{
  "join_code": "K3M7PQ", "kind": "listen",   // listen|send|intercom|ai_translate
  "language": "en",                            // 있을 때만
  "started_at": "2026-08-15T11:00:00Z", "ended_at": "...", "seconds": 1830,
  "participant_hash": "b616875b"               // 개인 식별 아님, 세션 dedup 용
}]}
```
- 201 `{"accepted": n, "skipped": n}` — field-server 의 로컬 원장 정리 판단 근거. [v1.1: 응답 바디 명시]
- Rails 는 join_code → service/church 를 해석해 `field_usage_records` 저장(유저별=church별, 시간대별 집계의 원천).

## 5. 기기 승인(디바이스 연동) — 앱 로그인 대체

1) `POST /api/app/device_links`  body `{"device_name": "iPhone 16 Pro"}` (무인증)
   → 201 `{"link_code": "AB34CD78", "poll_token": "<opaque 32자>", "expires_at": "<iso, 10분>"}`
2) 사용자가 웹(로그인 상태) **설정 → 기기 연동** 페이지에서 link_code 입력·승인
3) 앱 폴링 `GET /api/app/device_links/:poll_token` (3초 간격 권장)
   → `{"status":"pending"}` | `{"status":"approved","device_token":"<32자>","user":{"email":"...","church_name":"..."}}` | 404(만료·소진)
- `device_token` 은 신규 모델 `AppDevice`(user 귀속, has_secure_token, revoked_at) — 이후 앱 인증은 `Authorization: Bearer <device_token>`.
- link_code 는 EdgeDevice 클레임 코드 패턴(SHA256 digest 저장·TTL)을 따른다. poll_token 승인 응답은 1회만 device_token 을 내려준다.

## 6. 라이브 개설/관리 — 앱 (Bearer device_token)

- `POST /api/app/lives`  body `{"title":"...", "target_languages":["en","ja"], "intercom_enabled": true}`
  → 201 `{"service_id", "title", "join_code", "send_password", "transport":"sfu", "field_server_url", "field_ws_url", "source_language":{...}, "languages":[...]}` [v1.2: title 추가]
- `GET /api/app/lives` → 200 `{"lives": [{"service_id","title","join_code","status","started_at"}, ...]}` [v1.2: 래핑 키 `lives` 확정]
- `POST /api/app/lives/:id/stop` — **`:id` = `service_id`** → 200 `{"service_id","status","ended_at"}` [v1.2 확정]
- 401: device_token 무효/회수됨.
- 참고: 앱은 `field_ws_url` 을 직접 쓰지 않는다(LiveKit URL 은 field-server 토큰 응답 `grant.url` 로 수신). 값은 유지(웹·진단용).

## 6a. QR 페이로드 규약 [v1.3 개정 — 강의실 시나리오 확정]

- **수신용 QR**: `https://ev211.com/live/<접속코드>` — **유니버설/앱 링크**. 기본 카메라 스캔 시 앱 설치자는 앱이 열려 해당 라이브 참여, 미설치자는 브라우저로 열려 웹 청취 페이지로 리다이렉트. 웹·Edge·앱 개설 화면 모두 이 포맷으로 생성.
  - Rails: `GET /live/:code`(대소문자 무관) → 302 `/listen/<stream_key>`(웹 폴백), 404 시 안내.
  - iOS: Associated Domains `applinks:ev211.com` + `/.well-known/apple-app-site-association`(appID `M44T8AR75V.com.ev211.ev211Mobile`, paths `/live/*`).
  - Android: App Links intent-filter(autoVerify) + `/.well-known/assetlinks.json`(package `com.ev211.ev211_mobile`, 지문은 슈퍼어드민 설정 — Play 서명 지문은 콘솔에서 확인해 입력).
- **송신자용 QR**: `ev211field://live?code=<접속코드>&pw=<송신비번>` 유지 — 앱 전용. 송신 비번을 https URL 에 싣지 않는다(미설치 단말이 스캔하면 웹 서버 로그에 비번이 남기 때문).
- **인앱 스캐너**: 앱은 OS 카메라 외에 인앱 QR 스캐너(mobile_scanner)도 제공 [v1.3 결정 — iOS 배포 타깃 15.5 상향·카메라 권한 문구 수반]. 두 포맷(https·ev211field) 모두 해석.
- 구 스킴 `ev211field://live?code=...`(수신용)도 앱은 계속 해석한다(하위 호환).

## 6b. field-server 채널 프로비저닝 — 개설자 앱 책임 [v1.4 신설]

라이브 개설 시 선택한 AI 통역 언어의 실채널은 **개설자 앱이 송신 진입 시점에 field-server 에 만든다**(Rails 는 관여하지 않음).

1. Floor 보장: `POST /channels {channel_id: 0, language: <source>, label: "원어(한국어)"}` — 이미 있으면 그대로 사용
2. AI 채널: live.languages 각각 `POST /ai-channels {target_language: <code>, source_channel: 0}` — 이미 열려 있으면 중복 개설하지 않음(목록 조회 후 결정)
3. 개설자는 곧장 Floor(ch-00) 송신으로 진입한다 — 수동 채널 선택 화면을 거치지 않는다
- 인증은 전부 복합 Bearer. idle 가드(10분)로 닫힌 AI 채널은 송신 재진입 시 같은 절차로 재개설된다.
- 인간 통역사 송신 화면의 언어 선택지는 **라이브의 언어 목록(원어+통역 언어)으로 제한**한다 — 전체 ISO 목록(첫 항목 Abkhazian 폴백) 금지.

## 6c. AI 통역 언어 카탈로그 + 자막 [v1.5 신설]

**AI 언어 카탈로그(단일 소스)** [v1.5.1 개정 — 사용자 결정: 중국어는 지역 변형 코드]: 카탈로그 코드는 BCP-47 표기를 쓴다 — `es pt fr ja ru de ko hi id vi it en` + **`zh-CN`(중국어·보통화)**. field-server 가 모델 호출 시 `zh-CN → zh` 로 매핑한다(매핑 테이블은 field-server 소유). `zh-TW`(번체)·`zh-HK`(광둥어)는 실시간 모델이 음성 미지원이라 **카탈로그에 넣지 않는다**(조용한 실패 재발 방지) — Edge 정밀 경로(TTS 선택 가능)가 담당하고, 모델 지원 시 카탈로그에 추가한다.
- 앱 라이브 개설 화면의 AI 언어 선택지는 **이 13종(원어 제외)만** 노출한다. 전체 ISO 목록 금지.
- Rails `POST /api/app/lives` 는 카탈로그 밖 코드가 섞이면 **422 로 명시 거부**한다(조용한 필터 탈락 금지 — zh-CN 누락 사고의 재발 방지). 오류 바디에 어떤 코드가 거부됐는지 포함.

**자막(data track)**: AI 워커가 LiveKit data(topic `captions`, reliable)로 발행. 페이로드:
```json
{"channel_id": 1, "track_name": "ch-01", "kind": "source|target",
 "language": "en",           // kind=target 일 때만, source 는 null
 "seq": 12, "delta": "Hello "}
```
- sfu 수신 화면은 `captions` topic 을 구독해 **kind=target && language==선택 언어** 의 delta 를 seq 순으로 이어붙여 하단 자막으로 표시한다(롤링, 최근 발화 유지). 원문(source) 자막은 토글 옵션(기본 꺼짐).

## 7. field-server 측 (참고 — field-server 작업분)

- 송신 계열 인증: `FIELD_SEND_AUTH_MODE=callback` 시 §3 콜백으로 라이브별 비번 검증(5분 긍정 캐시).
- **복합 Bearer 규약 [v1.2 신설]**: callback 모드에서 클라이언트는 `Authorization: Bearer <join_code>:<send_password>` 로 보낸다(콜론 구분). field-server 가 분리해 §3 콜백으로 검증 — 어느 라이브의 요청인지 식별하기 위함(단일 룸 구조에서 라이브 식별자 전달 경로). 앱 field_api 는 password 인자에 복합 문자열을 넣으면 프로토콜 무변경.
- 무전기 상한: `INTERCOM_MAX_PARTICIPANTS` env 화 — 클라우드 100, 내부망 기본 8 유지.
- 사용량: 채널 close 시 §4 로 push(실패 시 로컬 원장 유지, 재전송 큐 없음).
