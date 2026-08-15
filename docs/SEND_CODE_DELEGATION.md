<!-- 클라우드 모드 C 송신 인증을 LAN 비밀번호에서 ev211.com 계정 귀속 송신 코드로 교체하는 위임 계약 설계 -->
# 계정 귀속 송신 코드 — ev211.com 위임 계약 (증분 2 ②, 설계)

결정 B(REALTIME_TRANSLATE_SFU_PLAN §10) 실행 설계. 구현은 후속 증분에서 Rails·field-api·앱 순으로 진행한다.

## 1. 목표

- 클라우드 field-server 의 송신 계열 인증(`publish-tokens`·`intercom*`·`ai-channels`)을
  **공유 LAN 비밀번호(FIELD_SEND_PASSWORD)** 에서 **ev211.com 계정에 귀속된 송신 코드**로 교체한다.
- 과금·사용량이 ev211.com 조직(Organization)에 귀속된다. Flutter 쪽 OAuth 로그인 없이
  코드 입력만으로 귀속을 성립시킨다(앱 UX 유지).
- 청취(subscribe)는 지금처럼 무인증 6자리 코드 입장 그대로 둔다.

## 2. 흐름

```
ev211.com 콘솔(조직 관리자)                 앱(송신자)                field-server
  1. 송신 코드 발급  ────────────────▶  2. 코드 입력(8자리)  ───▶  3. 코드 검증(ev211.com 콜백)
     (조직 귀속·만료·폐기 관리)                                      4. publish/intercom 토큰 발급
                                                                    5. 사용 이벤트 ev211.com 보고(집계)
```

## 3. 검증 방식 — 콜백 + 단기 캐시 (권장)

| 방식 | 장점 | 단점 |
|---|---|---|
| **A. 콜백 검증(권장)** — field-api 가 ev211.com `GET /api/field/send-codes/:code` 호출 | 실시간 폐기·만료 반영, Rails 단일 소스 | ev211.com 가용성 의존(→ 캐시로 완화) |
| B. 서명 코드(JWT/HMAC) 오프라인 검증 | ev211.com 다운에도 동작 | 즉시 폐기 불가(폐기 목록 동기화 필요), 코드가 길어짐 |

- 발급/입장 시에만 호출되므로 트래픽은 무시 수준. **긍정 결과 5분 캐시**(TTL)로 콜백 부담·순단을 흡수한다.
- 부정 결과(무효 코드)는 캐시하지 않는다(발급 직후 입장 실패 방지).
- ev211.com 콜백 인증: 헤더 `X-Field-Secret`(공유 시크릿, 양측 .env). HTTPS 필수.

## 4. 데이터 모델

### ev211.com (Rails — 새 마이그레이션)
```
field_send_codes
  organization_id  (belongs_to, 과금 귀속)
  code             (8자리 숫자, unique, 표시용 4-4 분절)
  label            (용도 메모: "본당 송신" 등)
  active           (boolean, 즉시 폐기 스위치)
  expires_at       (nullable — 상시 코드 허용)
  last_used_at / use_count  (관제용)
```
콘솔 UI: 조직 설정 → "필드 송신 코드" 섹션(발급·폐기·사용 이력). 슈퍼어드민 열람 가능.

### field-server (.env 추가)
```
EV211_API_BASE=https://ev211.com            # 콜백 대상
EV211_FIELD_SECRET=<공유 시크릿>            # 콜백 인증
FIELD_SEND_AUTH_MODE=password|code|both     # 마이그레이션 스위치(기본 password)
```

## 5. field-api 변경(후속 구현)

- `_auth_bearer(send_password)` 를 `_auth_send(request)` 로 일반화 — mode 에 따라
  비밀번호 비교 또는 코드 콜백 검증. `both` 는 코드 우선, 실패 시 비밀번호 폴백(전환기).
- 검증 성공 시 `organization_id` 를 signal_events 에 함께 기록(과금 집계 근거).
- 잠금·율제한 가드는 현행 그대로 재사용(코드 무차별 대입 방어).
- 사용 보고: 채널 close 시 `POST /api/field/usage`(조직·채널·시간·언어 수) — 실패해도
  로컬 원장(signal_events)이 남으므로 재전송 큐는 두지 않는다(초기).

## 6. 앱 변경(증분 3과 함께)

- 클라우드 서버 프로파일(`server_store`)에 "송신 코드" 필드 추가 — 모드 C 송신/무전기
  진입 시 비밀번호 대신 코드 입력. 모드 A(내부망)는 비밀번호 그대로.

## 7. 남은 결정

- [ ] 코드 자릿수(8자리 숫자 vs 영숫자) — 청취 6자리와 시각적으로 구분되게 8자리 권장.
- [ ] 상시 코드 허용 여부(expires_at null) — 교회 상설 장비용은 상시가 편함. 권장: 허용.
- [ ] ev211.com 다운 시 정책 — 캐시 미스면 fail-closed(발급 거부) 권장(과금 누수 방지).
