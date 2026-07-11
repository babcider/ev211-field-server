# LiveKit 토큰 발급 — subscribe(무인증·TTL 10분)·publish(비번·TTL 1시간 duplex) grant 생성
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from livekit.api import AccessToken, VideoGrants

from .config import (
    INTERCOM_TTL_SECONDS,
    MONITOR_TTL_SECONDS,
    PUBLISH_TTL_SECONDS,
    SUBSCRIBE_TTL_SECONDS,
)


def _expires_at(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


def room_name(generation: int) -> str:
    return f"field-g{generation}"


def intercom_room_name(generation: int) -> str:
    """인터컴(PTT) 기본 룸 — 예배 릴레이 룸(field-g*)과 격리해 강제·집계를 단순화한다.

    웹 송신자 인증 검증(POST /intercom-tokens) 및 채널 미지정 호환 경로가 쓴다.
    """
    return f"intercom-g{generation}"


def intercom_channel_room_name(generation: int, channel_id: int) -> str:
    """무전기 채널별 전용 룸(intercom-g<gen>-c<id>). 모두 intercom-g 접두사라

    부트스트랩·세대회전의 구세대 intercom-g* 폐기 규칙에 함께 포함된다.
    """
    return f"intercom-g{generation}-c{channel_id}"


def issue_subscribe_token(
    api_key: str, api_secret: str, generation: int, channel_id: int
) -> tuple[str, str]:
    """수신 토큰을 발급한다. 반환: (jwt, identity). identity=listener-<uuid>.

    canSubscribe=true 전용. 구독 트랙은 grant 로 강제할 수 없으므로 클라이언트 규약이다.
    """
    identity = f"listener-{uuid.uuid4()}"
    grants = VideoGrants(
        room=room_name(generation),
        room_join=True,
        can_subscribe=True,
        can_publish=False,
        can_publish_data=False,
    )
    token = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=SUBSCRIBE_TTL_SECONDS))
        .to_jwt()
    )
    return token, identity


def issue_publish_token(
    api_key: str, api_secret: str, generation: int, identity: str
) -> str:
    """송신 토큰을 발급한다. identity 는 lease 획득 시 결정된 규약 문자열.

    canPublishSources=[microphone] + canSubscribe=true(Floor duplex). 트랙명은
    grant 로 제한 불가하므로 채널 격리는 identity + 웹훅 강제로 달성한다.
    """
    grants = VideoGrants(
        room=room_name(generation),
        room_join=True,
        can_publish=True,
        can_subscribe=True,  # duplex — 통역사가 Floor(ch-00) 동시 수신
        can_publish_data=False,
        can_publish_sources=["microphone"],
    )
    return (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=PUBLISH_TTL_SECONDS))
        .to_jwt()
    )


def issue_monitor_token(
    api_key: str, api_secret: str, generation: int, target_room: str | None = None
) -> tuple[str, str]:
    """관리자 모니터 토큰을 발급한다. 반환: (jwt, identity). identity=monitor-<uuid>.

    hidden participant(다른 참가자에게 보이지 않음) + 발행 불가 + 구독 허용. LiveKit 은
    active-speaker(오디오 레벨) 신호를 subscriber 데이터채널로 보내므로 **구독 없이는
    레벨 신호가 오지 않는다**(E2E 검증). 클라이언트는 구독만 하고 재생(attach)하지
    않는 규약이다. 구독 자체는 무인증 subscribe 토큰으로도 가능하므로 권한 확장이
    아니다. monitor-* identity 는 웹훅 강제·집계 대상에서 제외된다(identity 규약).
    """
    identity = f"monitor-{uuid.uuid4()}"
    grants = VideoGrants(
        room=target_room or room_name(generation),
        room_join=True,
        can_subscribe=True,
        can_publish=False,
        can_publish_data=False,
        hidden=True,
    )
    token = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=MONITOR_TTL_SECONDS))
        .to_jwt()
    )
    return token, identity


def issue_recording_token(
    api_key: str, api_secret: str, target_room: str
) -> tuple[str, str]:
    """서버 녹음용 hidden·subscribe-only 토큰을 발급한다.

    identity를 monitor-* 규약에 포함해 릴레이 룸 웹훅 강제와 청취자 집계에서 제외한다.
    """
    identity = f"monitor-recorder-{uuid.uuid4()}"
    grants = VideoGrants(
        room=target_room,
        room_join=True,
        can_subscribe=True,
        can_publish=False,
        can_publish_data=False,
        hidden=True,
    )
    token = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grants)
        .with_ttl(timedelta(hours=24))
        .to_jwt()
    )
    return token, identity


def issue_intercom_token(
    api_key: str, api_secret: str, generation: int, name: str = "", room: str | None = None
) -> tuple[str, str, str]:
    """인터컴(PTT) 토큰을 발급한다. 반환: (jwt, identity, track_name).

    room 미지정이면 기본 룸 intercom-g<gen>(웹 인증 검증용), 지정하면 채널 룸
    intercom-g<gen>-c<id> 에 canPublish(마이크만)+canSubscribe 로 접속한다.
    identity=intercom-<uuid>, 트랙명 규약 ic-<uuid8> — 릴레이 룸의 ch-* 와 구분되며
    웹훅 강제는 릴레이 룸 스코프라 인터컴 룸에는 적용되지 않는다. name 은 참가자
    목록에 표시되는 별명(선택)이다.
    """
    suffix = uuid.uuid4()
    identity = f"intercom-{suffix}"
    track = f"ic-{str(suffix)[:8]}"
    grants = VideoGrants(
        room=room or intercom_room_name(generation),
        room_join=True,
        can_subscribe=True,
        can_publish=True,
        can_publish_data=False,
        can_publish_sources=["microphone"],
    )
    token = AccessToken(api_key, api_secret).with_identity(identity).with_grants(grants)
    if name:
        token = token.with_name(name)
    jwt = token.with_ttl(timedelta(seconds=INTERCOM_TTL_SECONDS)).to_jwt()
    return jwt, identity, track


def intercom_response(
    token: str,
    generation: int,
    ws_url: str,
    identity: str,
    track_name: str,
    room: str | None = None,
    channel_id: int | None = None,
) -> dict:
    body = {
        "token": token,
        "room": room or intercom_room_name(generation),
        "url": ws_url,
        "identity": identity,
        "track_name": track_name,
        "ttl_seconds": INTERCOM_TTL_SECONDS,
        "expires_at": _expires_at(INTERCOM_TTL_SECONDS),
    }
    if channel_id is not None:
        body["channel_id"] = channel_id
    return body


def monitor_response(
    token: str,
    generation: int,
    ws_url: str,
    identity: str,
    target_room: str | None = None,
    channel_id: int | None = None,
) -> dict:
    body = {
        "token": token,
        "room": target_room or room_name(generation),
        "url": ws_url,
        "identity": identity,
        "ttl_seconds": MONITOR_TTL_SECONDS,
        "expires_at": _expires_at(MONITOR_TTL_SECONDS),
    }
    if channel_id is not None:
        body["channel_id"] = channel_id
    return body


def subscribe_response(
    token: str, generation: int, channel_id: int, ws_url: str, listener_id: str
) -> dict:
    track = f"ch-{channel_id:02d}"
    return {
        "token": token,
        "listener_id": listener_id,
        "room": room_name(generation),
        "url": ws_url,
        "channel_id": channel_id,
        "track_name": track,
        "ttl_seconds": SUBSCRIBE_TTL_SECONDS,
        "expires_at": _expires_at(SUBSCRIBE_TTL_SECONDS),
    }


def publish_response(
    token: str,
    generation: int,
    channel_id: int,
    ws_url: str,
    identity: str,
    epoch: int,
) -> dict:
    track = f"ch-{channel_id:02d}"
    return {
        "token": token,
        "room": room_name(generation),
        "url": ws_url,
        "channel_id": channel_id,
        "track_name": track,
        "identity": identity,
        "epoch": epoch,
        "generation": generation,
        "ttl_seconds": PUBLISH_TTL_SECONDS,
        "expires_at": _expires_at(PUBLISH_TTL_SECONDS),
    }
