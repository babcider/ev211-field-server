<!-- EV211 Phase 0 LiveKit 스파이크 실측 결과 — 부하 테스트 수치, 병목, D1 판정 근거 -->
# Phase 0 — LiveKit 인프라 스파이크 실측 결과

측정일: 2026-07-10 · 환경: macOS(Darwin 25.5.0), Docker Desktop, Apple Silicon 맥북 단일 노드

## 1. 기동 검증

| 항목 | 결과 |
|---|---|
| `docker compose up -d` | 성공 (livekit v1.13.3, caddy 2.11.4-alpine) |
| LiveKit health | `healthy` (config 파싱 오류 1건 수정 후 — `audio.update_interval` 단위 ms 정수) |
| LiveKit HTTP `:7880/` | 200 |
| Caddy http `:8880/` | 200 |
| Caddy https `:8443/` (자체서명) | 200 (bare `:8443` → `localhost,127.0.0.1` 명시 호스트로 수정 후) |
| vendor JS 서빙 (http/https) | 200, 541,080 bytes (`livekit-client@2.20.1` UMD) |
| publish 토큰 grant | `room=field, canPublish=true, canSubscribe=false, canPublishSources=[microphone]` ✅ 오디오 스코프 정확 |
| subscribe 토큰 grant | `canPublish=false, canSubscribe=true` ✅ |

## 2. 부하 테스트 (lk load-test, 오디오 전용)

publisher 는 각 1개 오디오 트랙 발행 → subscriber 는 전체 트랙 구독 시도.

#### (A) 초기 측정 — UDP 범위 50000-50100 (101 포트, 202 매핑)

| # | audio-pub | subscribers | ramp | 전달 트랙 | timeout | 패킷손실(연결분) | LiveKit peak CPU |
|---|---|---|---|---|---|---|---|
| T1 | 15 | 50  | 5/s | 216 / 750  | 14  | 0% | (미측정) |
| T2 | 15 | 100 | 3/s | 216 / 1500 | 64  | 0% | **65.8%** |
| T3 | 15 | 200 | 2/s | 216 / 3000 | 164 | 0% | ~66% (동급) |

**핵심 관찰**: 세 테스트 모두 **정확히 216 트랙**에서 상한.
**해석 정정(codex 지적)**: 216 / 15트랙 = **14.4** 이다. 이는 "각 subscriber 가 15트랙 전체를 구독한다"는 lk load-test 모델에서 나온 수치로, **완전 구독에 성공한 subscriber ≈ 14명**을 뜻한다(이전 문서의 "36명"은 오류 — 정정). 램프 속도(5/s→2/s)를 늦춰도 상한 불변 → 램프 문제 아님. 연결에 성공한 subscriber 는 **패킷 손실 0%**, 오디오 정상 수신.

#### (B) 재측정 — UDP 범위 42000-47000 (5001 포트) · 이번 스파이크 수정 후

이번에 UDP 범위를 101 → 5001 포트로 넓히고(macOS ephemeral 충돌 회피 위해 42000-47000) 재검증했다.

| # | audio-pub | subscribers | duration | 전달 트랙 | 패킷손실(연결분) | LiveKit peak CPU | idle CPU/MEM |
|---|---|---|---|---|---|---|---|
| T4 | 15 | 100 | 30s | **600 / 1500** | 0% | (미측정) | 0.33% / 393MiB |

**핵심 관찰(새 근거)**: 동일 15pub×100sub 에서 전달 트랙이 216 → **600 으로 증가**. 각 sub 가 평균 6/15 트랙 구독 성공(≈40 sub 완전 구독 상당). 즉 **UDP 포트 범위를 넓히자 상한이 함께 위로 이동**했다 → "동시 UDP 플로우/포트 수"가 제약이라는 가설을 뒷받침하는 첫 실측 근거. **다만 비례는 아니다**: 포트는 101→5001(약 50배) 확대인데 트랙은 216→600(약 2.8배)만 늘었다. 상한이 포트 수에 정비례하지 않으며, 포트 외 다른 계층(프록시 CPU/fd, ICE 처리 등)도 함께 작용함을 시사한다. 또한 **600 트랙 시점의 LiveKit CPU 는 측정하지 못했다**(T4 에서 peak CPU 미수집). 연결분 패킷손실은 여전히 0%.

- LiveKit CPU 는 부하 중 최대 ~66%(초기 측정 T2, 단일 코어 수준), 종료 후 idle 0.33%.
- 메모리 ~250-490MiB.
- 종료 시 WARN 로그(`User Initiated Abort` = data channel abort)는 부하툴 종료 시 정상 teardown, 오류 아님. ICE 실패 로그는 관찰되지 않음.

### 측정 기준 대비 판정

| 기준 (계획서 Phase 0) | 목표 | 실측 | 판정 |
|---|---|---|---|
| p95 지연 | < 500ms | **이번 환경에서 측정 못 함**. lk load-test 가 참가자별 p95 요약을 출력하나 스크롤로 유실됐고, 별도 계측 파이프라인 없음. 연결분 패킷손실만 0% 확인. | 미측정 |
| 재버퍼(rebuffer) | — | **미측정** (부하툴이 재버퍼 지표를 별도 제공 안 함) | 미측정 |
| 접속 성공률 | > 99% | 초기 216/3000, 재측정 600/1500 — 어느 쪽도 목표 미달. macOS Docker 환경 상한(아래 가설) | ✗ (환경 제약, Linux 재검증 필요) |

## 3. 병목 분석 — 환경 vs LiveKit (가설, 미입증)

**가설(단정 아님): 상한을 만드는 것은 LiveKit 처리 용량이 아니라 macOS Docker Desktop 의 UDP 포트 매핑/프록시 계층으로 보인다.** 아래는 이를 뒷받침하는 정황이며, Linux host 재검증 전까지는 **미입증 가설**로 취급한다(이전 문서의 "결론/단정" 표현을 정정).

정황 근거:
1. LiveKit CPU 가 ~66% 에서 더 오르지 않고 상한이 고정 → 서버 처리 여력이 남아 있는데 신규 연결이 막힘(부하 후 idle 0.33%, mem 393MiB — 여유).
2. 램프 속도를 늦춰도(2/s) 동일 상한 → 순간 부하 문제 아님.
3. 연결 성공분은 패킷손실 0% → 미디어 경로 자체는 정상.
4. **UDP 범위를 101 → 5001 포트로 넓히자 상한이 216 → 600 트랙으로 함께 이동**(§2-B, 이번 실측). 상한이 UDP 포트 매핑 계층과 연동됨을 보이는 첫 직접 근거로, "동시 UDP 플로우/포트 매핑 수" 계층 제약 가설에 부합. **단 비례는 아님**(포트 ~50배 확대 대비 트랙 2.8배 증가), 포트 수만으로 상한이 정해지진 않는다. 600 트랙 시점 CPU 는 미측정.
5. macOS Docker Desktop 은 컨테이너 UDP 를 VM userland 프록시로 NAT(포트당 개별 매핑). 다수 동시 WebRTC ICE 플로우에서 병목이 될 수 있다는 것은 널리 보고된 제약이나, **본 스파이크에서 프록시 CPU/fd 를 직접 계측해 인과를 증명하지는 못했다**(수집 시도했으나 VM 내부 프로세스 지표 접근 제한).

즉 이 수치는 **스파이크 실행 환경(맥북+Docker Desktop 포트매핑)의 네트워크 계층 상한으로 추정**되며, LiveKit 용량 상한이라고 단정할 근거는 없다. 200 subscriber 완전 구독 검증은 이 환경에서 구조적으로 어렵다.

### 수집한 로그/지표 (이번 첨부)

- LiveKit 기동 로그: `nodeIP=192.168.219.127`, `rtc.portICERange=[42000,47000]`, keys 는 `LIVEKIT_KEYS` 환경변수 주입(평문 제거 확인), 기동 error 0.
- 부하 중/후 리소스: LiveKit idle CPU 0.33%, MEM 393MiB / Caddy CPU 0%, MEM 18MiB.
- 부하 종료 WARN: `User Initiated Abort`(data channel) 다수 — 정상 teardown. **ICE gathering/connection 실패 로그는 미관찰**.
- Docker 바인드 UDP 핸들 수: ~5571 (5001 포트 매핑 반영).
- (미수집) macOS Docker VM 내부 UDP 프록시 프로세스의 CPU/fd — VM 격리로 직접 접근 불가. Linux host 재검증 시 host 프로세스 지표로 대체 확보 예정.

## 4. 게이트 잔여 조건 (Linux 재검증) — D1 확정 전 필수

이번 스파이크는 macOS Docker Desktop 환경 상한으로 200sub 완전 검증을 못 했고, p95/재버퍼도 미측정이다. 아래를 Linux host 환경에서 수행해야 게이트를 통과한다.

**재검증 시나리오(제품 모델 근접):**
- 15 publisher(각 1 오디오 트랙) + **200 subscriber, 각 subscriber 트랙 1개만 구독**, 10분 지속.
  - ⚠ lk load-test 는 sub 별 트랙 1개 제한 옵션이 없다(§5 P4). 제품 모델(1트랙/sub) 검증은 **Phase 1 field-api + 실제 클라이언트 다중 인스턴스** 또는 커스텀 부하 클라이언트로 수행하거나, `--audio-publishers 1` 로 근사(각 sub 1트랙 구독)해 하한만 확인한다.
- 보존할 지표/로그: **접속 성공률, p95 지연, 재버퍼율, 패킷 손실, LiveKit CPU/MEM, ICE 실패 로그, host UDP 프록시 없음 확인(host network)**.

**환경 설정:**
- Linux 호스트 + `docker compose -f docker-compose.yml -f docker-compose.host.yml up -d`(host network, UDP 프록시 우회).
- `.env`: `FIELD_NODE_IP`=서버 LAN IP, `FIELD_UDP_START/END`=50000/60000(host 에선 충돌 없음).
- 방화벽: 7880/7881/tcp, 50000-60000/udp, 8880/8443/tcp 개방.

LiveKit 공식 벤치마크상 단일 노드 오디오 200 구독은 여유(오디오 트랙 대역폭·CPU 매우 낮음, T2 총 4.2mbps)로 예상되나, **위 지표를 실측해 확인 전까지 확정하지 않는다**.

## 5. 발견한 문제 / 리스크

| # | 문제 | 영향 | 대응 |
|---|---|---|---|
| P1 | `livekit.yaml` `audio.update_interval` 이 duration 문자열 거부(ms 정수여야) | 기동 실패 | 정수(500)로 수정 완료 |
| P2 | Caddy bare `https://:8443` 는 빈 SNI 로 internal CA 발급 실패(TLS alert) | https 접속 불가 | 명시 호스트(`localhost, 127.0.0.1`)로 수정 완료. **현장은 서버 고정 IP 추가 필요** |
| P3 | macOS Docker UDP 매핑 상한(가설) | 스파이크에서 200 완전 검증 불가 | Linux host 재검증 필요(§4). UDP 범위 확대로 216→600 이동 확인 |
| P4 | lk load-test 는 sub 별 트랙 1개 제한 옵션 없음(전 트랙 구독) | 제품 모델(1트랙/sub)을 부하툴로 재현 불가 | loadtest.sh 주석에 명시. 제품 모델 검증은 Phase 1 실클라이언트/커스텀 부하로 이관 |
| P5 | livekit.yaml 에 API 키 평문 존재 | 자격증명 노출 | 키 재발급 + `LIVEKIT_KEYS` 환경변수 주입으로 이전, .gitignore 추가(이번 수정) |
| P6 | `auto_create: true` → 폐기 JWT 로 룸 재생성 가능 | 세대 관리 취약 | `auto_create: false` + create-room.sh, 룸은 field-api(Phase 1) 관리(이번 수정) |
| R1 | 현장 AP client isolation 켜져 있으면 전 참가자 접속 불가 | 치명 | README 점검표 필수 항목화 |
| R2 | 자체서명 인증서 최초 수락 UX | 참가 마찰 | 루트 CA 신뢰 설치 절차 README 추가(macOS/Win/iOS/Android), IP 고정 전제 |
| R3 | macOS 시스템 curl(LibreSSL)이 IP-SNI 로 `tlsv1 internal error` | 검증 시 오탐 | 서버 정상(openssl s_client·브라우저는 정상). curl 클라이언트 버그, 서버 결함 아님 |

## 6. D1 (LiveKit 채택) 판정 의견

**채택 지지 (조건부).**

- 오디오 15채널 발행 + 다수 구독이 **기능적으로 정상 동작**: 룸 1개 + 트랙 15개 모델, 마이크 전용 토큰 스코프, 단일 트랙 구독·전환(이전 트랙 해제)까지 스파이크 페이지·부하툴로 확인.
- 연결 성공분 **패킷손실 0%**, LiveKit CPU 여유(66%) — 서버 성능은 문제 없음. 오디오는 대역폭(총 4.2mbps)·CPU 부담이 낮아 현장 200명은 충분히 현실적일 것으로 **추정**.
- 이번에 200을 못 채운 원인은 **LiveKit 한계가 아니라 macOS Docker Desktop 의 UDP 포트 매핑 상한으로 추정**된다(미입증 가설, §3). UDP 범위 확대로 상한이 216→600 으로 이동한 것이 이 가설의 첫 근거다. → **게이트 통과 조건은 §4(Linux host 200sub·1트랙/sub·10분, p95/재버퍼/성공률/ICE 로그 보존)의 재검증 1회.** 그 전까지 D1 은 **조건부** 지지에 머문다.
- Flutter(D5) 검증은 본 워커 범위 밖(별도). 브라우저 publish/subscribe 경로는 로컬 번들 SDK 로 확정.

남은 확정 작업: (1) §4 Linux host 재검증, (2) 브라우저 실기기 수동 오디오 왕복 1회, (3) Floor duplex(수신+송신 동시) 에코/AEC 검증(§9, Phase 1).

## 부록 — 컨테이너 상태 (down 하지 않음, 이번 스파이크 수정 후)

```
field-caddy     Up   0.0.0.0:8880->8880, 8443->8443 (env FIELD_NODE_IP=192.168.219.127)
field-livekit   Up (healthy)  7880-7881, 42000-47000/udp (5001 포트 매핑)
                nodeIP=192.168.219.127, rtc.portICERange=[42000,47000], keys=LIVEKIT_KEYS env(평문 제거)
idle CPU: livekit 0.33%, mem ~393MiB · caddy 0%, mem ~18MiB
룸: field (RM_BMZEkqaBfTNJ, auto_create=false → create-room.sh 로 생성)
```

## 부록 — 재검증 요약 (이번 스파이크 수정 후 재기동)

| 항목 | 결과 |
|---|---|
| `docker compose up -d --force-recreate` (새 키) | 성공, livekit **healthy** |
| LiveKit 기동 로그 | error 0, `nodeIP=192.168.219.127`, `portICERange=[42000,47000]`, 평문키 경고 없음 |
| Caddy http `:8880/` | 200 |
| Caddy https `localhost:8443/` | 200 |
| Caddy https `192.168.219.127:8443/`(서버 LAN IP) | **200** (openssl s_client 확인. 인증서 SAN=IP:192.168.219.127. macOS curl 은 IP-SNI 버그로 실패하나 서버 정상) |
| `/rtc` wss 프록시 → LiveKit signaling | **도달 확인**: `/rtc/validate` → 401(토큰 필요) = LiveKit 응답. http(:8880)·https(:8443 LAN IP) 양쪽 프록시 정상 |
| 룸 생성(create-room.sh) | 성공, 룸 `field` 존재 |
| 토큰 발급(새 키) | publish/subscribe 토큰 정상 발급 |
| 부하 재측정(15×100×30s) | 600/1500 트랙, 패킷손실 0%, ICE 실패 로그 없음 (§2-B) |
