# 환경변수 로딩·검증(비밀번호 미설정·기본값이면 기동 실패)과 상수 정의를 담당하는 설정 모듈
from __future__ import annotations

import os
from dataclasses import dataclass

# 계약 상수 — openapi.yaml 서두 규약과 일치시킨다.
MAX_CHANNELS_CAP = 15  # 일반 채널 최대 개수(채널 0 Floor는 별도)
FLOOR_CHANNEL_ID = 0
SUBSCRIBE_TTL_SECONDS = 600  # 수신 토큰 TTL 10분
PUBLISH_TTL_SECONDS = 3600  # 송신 토큰 TTL 1시간(= lease TTL)
MONITOR_TTL_SECONDS = 600  # 관리자 모니터 토큰 TTL 10분(만료 시 재발급·재접속)
INTERCOM_TTL_SECONDS = 3600  # 인터컴(PTT) 토큰 TTL 1시간(끊기면 재발급·재접속)
INTERCOM_MAX_PARTICIPANTS = 8  # 인터컴 룸 참가 상한 기본값(FIELD_INTERCOM_MAX 로 오버라이드, 상한 100)
INTERCOM_MAX_PARTICIPANTS_CAP = 100  # env 오버라이드 허용 상한(클라우드 무전기 100명 — LIVE_PLAN §4)
INTERCOM_MAX_CHANNELS = 8  # 무전기 채널 최대 개수(0~7)
LISTENER_HEARTBEAT_TTL_SECONDS = 30  # heartbeat 보관 TTL 30초
ISSUED_LISTENER_TTL_SECONDS = 3600  # 발급 원장·토큰 근사 카운트 보관 TTL 1시간(만료분 정리)
LEASE_JOIN_GRACE_SECONDS = 120  # 미접속(joined 안 됨) lease 자동 해제 유예(발급 후 접속 실패 방어)
ROOM_EMPTY_TIMEOUT_SECONDS = 86400  # 룸 empty_timeout — 빈 룸이 삭제되지 않도록 충분히 크게(24h)
SIGNAL_LOG_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 송수신 세션 이벤트 보관 기간 30일
SIGNAL_LOG_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # 만료 이벤트 정리 주기 1일

# ---- 모드 C AI 통역(gpt-realtime-translate) ----
AI_TRANSLATE_MODEL = "gpt-realtime-translate"  # OpenAI 번역 전용 realtime 모델 id
AI_TRANSLATE_INPUT_MODEL = "gpt-realtime-whisper"  # 세션 내부 입력 전사 모델
AI_TRANSLATE_URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
AI_INPUT_SAMPLE_RATE = 24_000  # OpenAI 입출력 PCM16 mono 표본율(24kHz 고정)
AI_FLOOR_SAMPLE_RATE = 48_000  # LiveKit Floor(ch-00) 원음 표본율
# 세션 60분 한계 대비 T-5분 make-before-break 갱신 지점(둘째 WS 워밍 → 원자 스위치).
AI_SESSION_RENEW_SECONDS = 55 * 60
AI_SESSION_RENEW_CHECK_SECONDS = 10.0  # 갱신 시점 도달 여부 점검 주기
AI_SESSION_DRAIN_SECONDS = 8.0  # 스위치 후 구세션 잔여 출력 수거 시간(이후 close)
AI_SESSION_RENEW_RETRY_SECONDS = 30.0  # 갱신 실패 시 재시도 간격(구세션은 계속 사용)
AI_CAPTION_TOPIC = "captions"  # 자막 data 패킷 topic(청취 앱이 이 topic 으로 필터)
AI_WORKER_BACKOFF_BASE_SECONDS = 1.0  # 워커 크래시 재시작 백오프 초기값
AI_WORKER_BACKOFF_MAX_SECONDS = 30.0  # 백오프 상한
AI_WORKER_STOP_TIMEOUT_SECONDS = 5.0  # 워커 stop 1건당 상한(한 워커 지연이 전체 종료를 막지 않게)
AI_READY_TIMEOUT_SECONDS = 8.0  # AI 채널 개설 시 워커 최초 접속 준비 대기 상한(초과 시 503 롤백)
# 비용 가드: 원음(Floor)이 이 시간 동안 한 프레임도 오지 않으면 AI 채널을 자동 종료한다.
# 워커는 무음까지 OpenAI 로 계속 append 하므로(서버 VAD 계약), 송신을 켜둔 채 방치하거나
# 채널을 닫지 않으면 토큰이 계속 나간다. 발행자가 사라진 채널은 서버가 스스로 닫는다.
AI_IDLE_CLOSE_SECONDS = 10 * 60
AI_IDLE_CHECK_SECONDS = 60.0  # idle 스윕 주기
# 무음 구간 append 게이팅(T1) — 발행 중이지만 아무도 말하지 않는 시간의 OpenAI 송신을 멈춘다.
# idle 가드(위)는 '발행자가 사라진' 채널만 닫으므로, 발행 중 침묵(설교 사이 정적·준비 시간)은
# 그대로 과금된다. 게이팅은 **OpenAI append 만** 멈추고 Floor 프레임 수신 기록
# (last_floor_frame_at → floor_idle_seconds)과 세션은 그대로 유지한다.
AI_GATE_ENABLED = True  # False 면 기존처럼 무음까지 연속 append(회귀 시 즉시 되돌리는 스위치)
# 이 RMS(PCM16) 이하를 '무음'으로 본다. E2E 하니스의 SILENCE_RMS(200)보다 낮게 잡아
# 조용한 발화가 잘리지 않게 한다(끊김보다 비용 낭비가 안전 — 보수적 기본값).
AI_GATE_RMS_THRESHOLD = 150.0
# 무음이 이만큼 이어져야 게이트를 닫는다(짧은 문장 사이 호흡으로 닫히지 않게 넉넉히).
AI_GATE_HANGOVER_SECONDS = 1.5
# 게이트가 닫힌 동안 유지하는 선행 버퍼 — 소리가 돌아오면 이 구간을 함께 보내
# 발화 첫머리(자음 어택)가 잘리지 않게 한다.
AI_GATE_PREBUFFER_SECONDS = 0.4
# gpt-realtime-translate 가 출력 가능한 언어 13종(소문자 ISO). 입력은 70+ 언어.
AI_SUPPORTED_OUTPUT_LANGUAGES = frozenset(
    {"es", "pt", "fr", "ja", "ru", "zh", "de", "ko", "hi", "id", "vi", "it", "en"}
)
# 카탈로그(BCP-47) → 모델 코드 매핑(계약 §6c v1.5.1). 채널 표기는 카탈로그 코드를 유지하고
# gpt-realtime-translate 세션에만 모델 코드를 넘긴다. zh-TW 등 모델 미지원 변형은 카탈로그에 없다.
AI_MODEL_LANGUAGE_MAP = {"zh-CN": "zh"}
# 외부(앱·Rails)에 허용하는 카탈로그 코드 = 모델 13종 + 매핑 키. "zh" 직접 지정도 하위호환 허용.
AI_CATALOG_LANGUAGES = frozenset(AI_SUPPORTED_OUTPUT_LANGUAGES | set(AI_MODEL_LANGUAGE_MAP))

# 사용량 보고(계약 §4) — 종료된 세션을 ev211.com 으로 배치 push 하는 주기.
USAGE_PUSH_INTERVAL_SECONDS = 60.0
# 이탈 웹훅 유실·재시작으로 열린 채 남은 세션의 마감 상한. 예배·강의 길이(최대 수 시간)를
# 넉넉히 덮되, 무한히 열린 세션이 보고되지 않고 남는 것을 막는다.
USAGE_SESSION_MAX_SECONDS = 6 * 60 * 60
# 보고 완료된 세션 보관 기간(감사 원장은 signal_events 가 30일 보관한다).
USAGE_SESSION_RETENTION_SECONDS = 7 * 24 * 60 * 60

# 기본값(변경 강제 대상). .env.example 의 자리표시자를 기동 시 거부한다.
_DEFAULT_PASSWORDS = {"change-me", "changeme", "", "password", "default"}

# rate limit / 잠금 파라미터
SUBSCRIBE_RL_PER_MINUTE = 10  # subscribe-token 발급 IP당 분당 10회(계약 명시)
HEARTBEAT_RL_PER_MINUTE = 12  # heartbeat listener당 분당 상한(재보고 주기 10~15초 여유)
PUBLISH_FAIL_LIMIT = 5  # 연속 비밀번호 실패 잠금 임계
PUBLISH_LOCK_SECONDS = 300  # 잠금 지속(초)
ADMIN_FAIL_LIMIT = 5
ADMIN_LOCK_SECONDS = 300
PUBLISH_RL_PER_MINUTE = 20  # publish-token 발급 IP당 분당 상한
# 대시보드가 10초 폴링(상태 3종 = 분당 18회)을 상시 소모하므로 30이면 채널 연속
# 삭제(건당 삭제+새로고침 4회) 몇 번에 바닥나 정상 조작·로그인까지 막힌다(실운영 결함).
# 무차별 대입 방어는 ADMIN_FAIL_LIMIT/LOCK(비밀번호 실패 잠금)이 담당한다.
ADMIN_RL_PER_MINUTE = 120  # admin(status·takeover) IP당 분당 상한


@dataclass(frozen=True)
class Settings:
    """기동 시 1회 로딩하는 불변 설정."""

    livekit_api_key: str
    livekit_api_secret: str
    livekit_host: str  # RoomService/HTTP API 접속 URL(서버→LiveKit)
    livekit_rtc_url: str  # 서버 녹음 participant가 접속할 signaling URL
    ws_url: str  # 클라이언트에 반환할 signaling base URL
    send_password: str
    admin_password: str
    db_path: str
    recordings_path: str
    max_channels: int
    forwarded_allow_ips: str  # 신뢰 프록시 IP/CIDR(콤마 구분). X-Forwarded-* 신뢰 판정용.
    # 인터컴(무전기) 참가 상한 — 내부망 기본 8, 클라우드는 FIELD_INTERCOM_MAX=100 으로 확대.
    intercom_max: int = INTERCOM_MAX_PARTICIPANTS
    # 송신 계열 인증 모드(라이브 API 계약 §7): password(전역 비번)|callback(ev211.com 위임)|both.
    send_auth_mode: str = "password"
    # callback 모드에서 verify_send 콜백 대상·시크릿(양측 .env 동일값).
    ev211_api_base: str = ""
    field_callback_secret: str = ""
    # 모드 C AI 통역 워커가 gpt-realtime-translate WS 에 붙을 때 쓰는 키(선택).
    # 미설정이면 AI 채널 생성만 거부되고 나머지 기능은 정상 동작한다.
    openai_api_key: str = ""


class ConfigError(RuntimeError):
    """설정 검증 실패(기동 중단 사유)."""


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """환경변수에서 설정을 로딩하고 계약이 요구하는 기동 전제조건을 검증한다.

    - FIELD_SEND_PASSWORD / FIELD_ADMIN_PASSWORD 미설정·기본값이면 ConfigError.
    - 두 비밀번호가 같으면 ConfigError(계약: 서로 달라야 한다).
    - LiveKit API 키/시크릿 미설정이면 ConfigError.
    """
    e = env if env is not None else os.environ

    send_pw = (e.get("FIELD_SEND_PASSWORD") or "").strip()
    admin_pw = (e.get("FIELD_ADMIN_PASSWORD") or "").strip()

    if not send_pw or send_pw.lower() in _DEFAULT_PASSWORDS:
        raise ConfigError("FIELD_SEND_PASSWORD 가 미설정이거나 기본값입니다. 기동을 중단합니다.")
    if not admin_pw or admin_pw.lower() in _DEFAULT_PASSWORDS:
        raise ConfigError("FIELD_ADMIN_PASSWORD 가 미설정이거나 기본값입니다. 기동을 중단합니다.")
    if send_pw == admin_pw:
        raise ConfigError("FIELD_SEND_PASSWORD 와 FIELD_ADMIN_PASSWORD 는 서로 달라야 합니다.")

    api_key = (e.get("LIVEKIT_API_KEY") or "").strip()
    api_secret = (e.get("LIVEKIT_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise ConfigError("LIVEKIT_API_KEY / LIVEKIT_API_SECRET 가 필요합니다.")

    node_ip = (e.get("FIELD_NODE_IP") or "127.0.0.1").strip()
    # 서버→LiveKit HTTP API 접속 주소(내부 도커 네트워크에서는 http://livekit:7880).
    livekit_host = (e.get("LIVEKIT_HOST") or "http://livekit:7880").strip()
    livekit_rtc_url = (e.get("LIVEKIT_RTC_URL") or "ws://livekit:7880").strip()
    # 클라이언트(모바일 앱)에 반환하는 signaling base URL(#3). 기본은 평문 ws://<IP>:7880
    # 직결 — LiveKit SDK 는 자체서명 wss 의 TOFU 예외를 공유하지 않아 CA 미설치 단말이
    # wss://IP:8443 연결에 실패하기 때문. signaling 에는 비밀번호가 실리지 않고 단기 JWT 만
    # 노출되며, 미디어는 WebRTC DTLS-SRTP 로 항상 암호화된다. 미설정 시 노드 IP 기반 폴백.
    ws_url = (e.get("FIELD_WS_URL") or f"ws://{node_ip}:7880").strip()

    db_path = (e.get("FIELD_DB_PATH") or "/data/field.db").strip()
    recordings_path = (e.get("FIELD_RECORDINGS_PATH") or "/data/recordings").strip()

    # AI 통역 워커용 OpenAI 키(선택). 없으면 AI 채널 생성 요청만 503 으로 거부한다.
    openai_api_key = (e.get("OPENAI_API_KEY") or "").strip()

    # 신뢰하는 프록시(Caddy) IP/CIDR 목록. X-Forwarded-For/Proto 는 이 출처에서 온 요청에만 신뢰한다.
    # 도커 기본 브리지 서브넷을 폭넓게 신뢰(내부망 단일 노드 배포 전제).
    forwarded_allow = (
        e.get("FIELD_FORWARDED_ALLOW_IPS")
        or "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    ).strip()

    try:
        max_channels = int(e.get("MAX_CHANNELS") or MAX_CHANNELS_CAP)
    except ValueError as exc:
        raise ConfigError("MAX_CHANNELS 는 정수여야 합니다.") from exc
    if not 1 <= max_channels <= MAX_CHANNELS_CAP:
        raise ConfigError(f"MAX_CHANNELS 는 1..{MAX_CHANNELS_CAP} 범위여야 합니다.")

    try:
        intercom_max = int(e.get("FIELD_INTERCOM_MAX") or INTERCOM_MAX_PARTICIPANTS)
    except ValueError as exc:
        raise ConfigError("FIELD_INTERCOM_MAX 는 정수여야 합니다.") from exc
    if not 2 <= intercom_max <= INTERCOM_MAX_PARTICIPANTS_CAP:
        raise ConfigError(f"FIELD_INTERCOM_MAX 는 2..{INTERCOM_MAX_PARTICIPANTS_CAP} 범위여야 합니다.")

    send_auth_mode = (e.get("FIELD_SEND_AUTH_MODE") or "password").strip().lower()
    if send_auth_mode not in ("password", "callback", "both"):
        raise ConfigError("FIELD_SEND_AUTH_MODE 는 password|callback|both 중 하나여야 합니다.")
    ev211_api_base = (e.get("EV211_API_BASE") or "").strip()
    field_callback_secret = (e.get("FIELD_CALLBACK_SECRET") or "").strip()
    if send_auth_mode in ("callback", "both") and (not ev211_api_base or not field_callback_secret):
        raise ConfigError(
            "FIELD_SEND_AUTH_MODE=callback|both 에는 EV211_API_BASE 와 FIELD_CALLBACK_SECRET 가 필요합니다."
        )

    return Settings(
        livekit_api_key=api_key,
        livekit_api_secret=api_secret,
        livekit_host=livekit_host,
        livekit_rtc_url=livekit_rtc_url,
        ws_url=ws_url,
        send_password=send_pw,
        admin_password=admin_pw,
        db_path=db_path,
        recordings_path=recordings_path,
        max_channels=max_channels,
        forwarded_allow_ips=forwarded_allow,
        openai_api_key=openai_api_key,
        intercom_max=intercom_max,
        send_auth_mode=send_auth_mode,
        ev211_api_base=ev211_api_base,
        field_callback_secret=field_callback_secret,
    )
