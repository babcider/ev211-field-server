<!-- EV211 field-api 구현 안내 — 실행·구조·계약 대비 구현 편차 -->
# field-api 개발 안내

`openapi.yaml` 계약을 FastAPI로 구현한 내부망 현장 서버 제어 평면이다. 순수 중계만
담당하며 오디오 미디어는 LiveKit(SFU)이 중계한다. 이 서비스는 채널 원장·토큰 발급·
LiveKit 웹훅 기반 서버측 강제·상태 집계를 담당한다.

## 파일 구조

```
api/
  app/
    config.py          # 환경변수 로딩·검증(비번 미설정·기본값이면 기동 실패)
    db.py              # SQLite(WAL) 원장 — 채널·lease·세대·웹훅 멱등·listener/heartbeat
    identity.py        # speaker-ch-NN-e<epoch>-g<gen>-n<nonce> 파싱·검증
    tokens.py          # LiveKit subscribe/publish 토큰 발급(grant)
    livekit_client.py  # RoomService 래퍼(룸 생성/삭제·participant 제거·목록)
    monitor.py         # on-air 근사(구현 편차, 아래 참조)
    rate_limit.py      # rate limit·실패 잠금·반복 위반 차단 목록(메모리)
    state.py           # 전역 상태·기동 시퀀스(세대 결정·룸 reconcile)
    webhook.py         # 웹훅 서명 검증·강제 로직
    main.py            # FastAPI 라우트·앱 팩토리
  tests/               # pytest(LiveKit API 는 mock)
  Dockerfile
  requirements.txt
```

## 로컬 실행

```bash
cd api
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# 필수 환경변수(미설정·기본값이면 기동 실패):
export FIELD_SEND_PASSWORD=... FIELD_ADMIN_PASSWORD=...   # 서로 달라야 함
export LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
export LIVEKIT_HOST=http://localhost:7880 FIELD_WS_URL=ws://localhost:7880
export FIELD_DB_PATH=./field.db
.venv/bin/uvicorn app.main:app --port 8000
```

## 테스트·린트

```bash
.venv/bin/python -m pytest -q
```

## 기동 시퀀스 (state.bootstrap)

1. `FIELD_SEND_PASSWORD`/`FIELD_ADMIN_PASSWORD` 미설정·기본값·동일값이면 **기동 실패**.
2. 두 비밀번호 해시를 원장의 이전 해시와 비교해 세대 값 결정(변경 시 +1).
3. LiveKit에 `field-g<gen>` 룸 생성, 구세대 `field-g*` 룸 삭제(reconcile).
4. 메모리 차단 목록·on-air 상태 초기화.

## 계약 대비 구현 편차 (정직한 명시)

### 1. on-air 판정 — 숨김 모니터 participant 미구현, lease+track_published 근사로 대체

계약(openapi.yaml "on-air 판정 — 숨김 모니터 participant")은 field-api가 각 세대 룸에
`monitor-<uuid>`(`canSubscribe`만·`canPublish=false`) 숨김 participant로 상주하며
LiveKit의 `active_speakers`/`audioLevel` 이벤트로 **발화(오디오 레벨) 기반** on-air를
산출하도록 규정한다.

**현재 구현은 이 숨김 모니터 participant를 두지 않는다.** 대신 `track_published` /
`participant_left` 웹훅으로 "트랙 발행 중 여부"를 추적해 on_air를 **근사**한다
(`app/monitor.py`).

- 사유: 서버 프로세스에 `livekit-rtc`로 실제 WebRTC 참가자를 상주시키는 것은 네이티브
  미디어 스택 의존·연결/재연결 불안정 리스크가 크다. 계약 §5(작업지시)는 "livekit-rtc
  통합이 과도하게 불안정하면 on_air를 lease+track_published 기반 근사로 구현하되 편차를
  README에 명시"하는 것을 명시적으로 허용한다.
- **한계**: 오디오 레벨을 보지 못하므로 트랙을 발행 중이면 무음이어도 `on_air=true`로
  보고한다(무음 마이크 오탐 가능). on_air는 표시용 상태이며 채널 점유(lease) 판정과
  무관하므로 중복 송신 거부·격리 로직에는 영향이 없다.
- **후속 과제**: `livekit-rtc` 기반 숨김 모니터 participant를 도입해 `active_speakers`
  이벤트로 발화 기반 on_air를 산출한다. 도입 시 `monitor.py`의 근사 로직을 교체하고
  `webhook.py`의 on-air 갱신 호출을 제거하면 된다(모니터 identity는 이미 강제·집계에서
  제외 처리됨).

이 외 계약 규칙(identity 규약, atomic lease, nonce 재사용 방지, participant_joined 즉시
검증, track 1개 제한, 웹훅 서명 검증·멱등, 차단 목록, 세대 무효화, heartbeat 집계·rate
limit, 429/423)은 계약대로 구현했다.

## webhook api_key 주의

`livekit.yaml`의 `webhook.api_key`는 `.env`의 `LIVEKIT_API_KEY`(키 이름)와 일치해야
한다. LiveKit이 이 키/시크릿으로 웹훅 Authorization JWT를 서명하고, field-api가
동일 키/시크릿으로 검증한다. 컨테이너 entrypoint가 환경변수로 템플릿을 렌더링하므로
키를 설정 파일에 직접 적지 않는다.
