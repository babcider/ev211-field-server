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
INTERCOM_MAX_PARTICIPANTS = 8  # 인터컴 룸 참가 상한(발급 시점 검사)
INTERCOM_MAX_CHANNELS = 8  # 무전기 채널 최대 개수(0~7)
LISTENER_HEARTBEAT_TTL_SECONDS = 30  # heartbeat 보관 TTL 30초
ISSUED_LISTENER_TTL_SECONDS = 3600  # 발급 원장·토큰 근사 카운트 보관 TTL 1시간(만료분 정리)
LEASE_JOIN_GRACE_SECONDS = 120  # 미접속(joined 안 됨) lease 자동 해제 유예(발급 후 접속 실패 방어)
ROOM_EMPTY_TIMEOUT_SECONDS = 86400  # 룸 empty_timeout — 빈 룸이 삭제되지 않도록 충분히 크게(24h)
SIGNAL_LOG_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 송수신 세션 이벤트 보관 기간 30일
SIGNAL_LOG_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # 만료 이벤트 정리 주기 1일

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
ADMIN_RL_PER_MINUTE = 30  # admin(status·takeover) IP당 분당 상한


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
    )
