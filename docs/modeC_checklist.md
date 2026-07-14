<!-- 모드 C(ev211서버접속) 3-모드 확장 개발 체크리스트 — REALTIME_TRANSLATE_SFU_PLAN.md 실행용 -->
# 모드 C 확장 체크리스트

계획: `docs/REALTIME_TRANSLATE_SFU_PLAN.md` · 결정 A/B/C/D 확정(§10).

## 증분 1 — AI 번역 워커 코어 (서버, 로컬 검증 가능) ✅ 완료(브랜치, main 미푸시)
- [x] OpenAI gpt-realtime-translate WS 계약 조사(번역 전용 `session.*` 스키마·24kHz PCM16·60분 한계)
- [x] LiveKit Python 1.1.13 서브스크라이브(원음 PCM)·AudioSource publish 패턴 조사(recording.py 참조)
- [x] `translate_worker.py` — ch-00 구독 → OpenAI WS → ch-NN republish, 세션 갱신 훅(구조)
- [x] `db.py` 마이그레이션 — channels.source/target_language 방어적 ADD(멱등·동시기동 안전)
- [x] field-api 엔드포인트 — `POST/DELETE/GET /ai-channels` + 워커 슈퍼바이저(백오프·leaked 수거)
- [x] pytest(OpenAI·LiveKit mock) 그린 — **203 passed**
- [x] codex 검수 3회전(11→4→0 수렴) → 수정 반영 → pytest 재그린

### 증분 1 후속(Phase C~D로 이월)
- [ ] make-before-break 오디오 무중단 연속성(둘째 WS 워밍→원자 스위치)
- [ ] 자막 data track 발행(현재 on_transcript 콜백까지)
- [ ] 서버 재시작 시 AI 채널 워커 자동 복원(현재 고아 채널 원자적 close만)
- [ ] `openapi.yaml`에 `/ai-channels` 3종 계약 반영
- [ ] 실 OpenAI 키 + 라이브 LiveKit E2E로 이벤트 스키마·오디오 포맷·VAD 재검증

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
