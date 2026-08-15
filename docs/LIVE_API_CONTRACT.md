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
  → 201 `{"service_id", "join_code", "send_password", "transport":"sfu", "field_server_url", "field_ws_url", "source_language":{...}, "languages":[...]}`
- `GET /api/app/lives` → 내 교회의 최근 라이브 목록(제목·코드·상태·started_at)
- `POST /api/app/lives/:id/stop` → 200 (라이브 종료)
- 401: device_token 무효/회수됨.

## 7. field-server 측 (참고 — field-server 작업분)

- 송신 계열 인증: `FIELD_SEND_AUTH_MODE=callback` 시 §3 콜백으로 라이브별 비번 검증(5분 긍정 캐시).
- 무전기 상한: `INTERCOM_MAX_PARTICIPANTS` env 화 — 클라우드 100, 내부망 기본 8 유지.
- 사용량: 채널 close 시 §4 로 push(실패 시 로컬 원장 유지, 재전송 큐 없음).
