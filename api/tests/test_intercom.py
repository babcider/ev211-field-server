# 인터컴(PTT) 테스트 — 토큰 grant·인증·상한·fail-closed·룸 회전·웹훅 룸 스코프
from __future__ import annotations

import asyncio

import jwt
from livekit.api import WebhookEvent
from livekit.protocol.models import ParticipantInfo, Room, TrackInfo

from app.config import INTERCOM_MAX_PARTICIPANTS
from app.tokens import intercom_room_name, issue_intercom_token
from app.webhook import WebhookProcessor
from tests.conftest import API_KEY, API_SECRET


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def _event(kind: str, identity: str, event_id: str, room: str, track_name=None) -> WebhookEvent:
    ev = WebhookEvent(event=kind, id=event_id)
    ev.room.CopyFrom(Room(name=room))
    ev.participant.CopyFrom(ParticipantInfo(identity=identity))
    if track_name is not None:
        ev.track.CopyFrom(TrackInfo(name=track_name))
    return ev


# ---- 토큰 grant ----


def test_intercom_token_grant_publish_subscribe_mic_only():
    token, identity, track = issue_intercom_token(API_KEY, API_SECRET, 1, "통역사1")
    assert identity.startswith("intercom-")
    assert track.startswith("ic-") and len(track) == 11
    claims = _decode(token)
    video = claims["video"]
    assert video["room"] == "intercom-g1"
    assert video.get("canPublish") is True
    assert video.get("canSubscribe") is True
    assert video.get("canPublishSources") == ["microphone"]
    assert not video.get("hidden", False)
    assert claims["sub"] == identity
    assert claims.get("name") == "통역사1"


def test_intercom_room_name_follows_generation():
    assert intercom_room_name(3) == "intercom-g3"
    token, _i, _t = issue_intercom_token(API_KEY, API_SECRET, 3)
    assert _decode(token)["video"]["room"] == "intercom-g3"


# ---- 엔드포인트 ----


def test_intercom_endpoint_requires_send_password(client, admin_headers):
    r = client.post("/intercom-tokens")
    assert r.status_code == 401
    # 관리자 비밀번호로는 발급 불가(송신자 전용).
    r = client.post("/intercom-tokens", headers=admin_headers)
    assert r.status_code == 401


def test_intercom_endpoint_https_required(http_client, send_headers):
    r = http_client.post(
        "/intercom-tokens",
        headers={**send_headers, "X-Forwarded-For": "192.168.0.55"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "https_required"


def test_intercom_endpoint_issues_token_and_creates_room(client, send_headers, state):
    r = client.post("/intercom-tokens", json={"name": "통역사1"}, headers=send_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == f"intercom-g{state.generation}"
    assert body["url"] == "ws://192.168.0.10:7880"
    assert body["identity"].startswith("intercom-")
    assert body["track_name"].startswith("ic-")
    assert body["ttl_seconds"] == 3600
    # ensure_intercom_room 이 룸을 lazy 생성한다.
    assert body["room"] in state.livekit.rooms
    assert _decode(body["token"])["name"] == "통역사1"


def test_intercom_endpoint_body_optional(client, send_headers):
    r = client.post("/intercom-tokens", headers=send_headers)
    assert r.status_code == 200


def test_intercom_room_created_with_hard_cap(client, send_headers, state):
    """사용자 8명과 관리자 모니터·녹음 2명만 LiveKit 룸에 들어갈 수 있다."""
    r = client.post("/intercom-tokens", headers=send_headers)
    assert r.status_code == 200
    assert state.livekit.room_caps[state.intercom_room] == INTERCOM_MAX_PARTICIPANTS + 2
    # 릴레이 룸에는 상한이 없다(청취자 수백 명).
    assert state.livekit.room_caps.get(state.room, 0) == 0


def test_intercom_endpoint_full_returns_409(client, send_headers, state):
    room = state.intercom_room
    state.livekit.participants[room] = [
        ParticipantInfo(identity=f"intercom-p{i}") for i in range(INTERCOM_MAX_PARTICIPANTS)
    ]
    r = client.post("/intercom-tokens", headers=send_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "intercom_full"


def test_intercom_endpoint_fail_closed_on_livekit_error(client, send_headers, state):
    # 참가자 수 조회 실패 시 상한 우회를 막기 위해 502 로 거부한다(fail-closed).
    async def _boom(room):
        raise RuntimeError("livekit down")

    state.livekit.list_participants = _boom
    r = client.post("/intercom-tokens", headers=send_headers)
    assert r.status_code == 502
    assert r.json()["code"] == "livekit_error"


def test_intercom_room_rotates_with_send_password(client, send_headers, state):
    # 회전 전 인터컴 룸 생성.
    r = client.post("/intercom-tokens", headers=send_headers)
    assert r.status_code == 200
    old_room = r.json()["room"]
    assert old_room in state.livekit.rooms
    # 송신 비밀번호 회전 → 구세대 인터컴 룸 삭제, 새 발급은 새 세대 룸.
    _run(state.change_send_password("rotated-send-pw"))
    assert old_room not in state.livekit.rooms
    r2 = client.post(
        "/intercom-tokens", headers={"Authorization": "Bearer rotated-send-pw"}
    )
    assert r2.status_code == 200
    assert r2.json()["room"] == f"intercom-g{state.generation}"
    assert r2.json()["room"] != old_room


def test_bootstrap_purges_all_intercom_rooms(state):
    """재시작 시 인터컴 룸을 세대 무관 전부 폐기한다(18차 #3 — 무상한 구 룸 잔존 제거)."""
    # 상한 없는(구 빌드·수동 생성) 현재 세대 인터컴 룸이 남아 있는 상황을 재현.
    state.livekit.rooms.append(state.intercom_room)
    state.livekit.room_caps[state.intercom_room] = 0
    state.livekit.rooms.append("intercom-g1")
    _run(state.bootstrap())
    assert state.intercom_room not in state.livekit.rooms
    assert "intercom-g1" not in state.livekit.rooms
    # 릴레이 룸은 유지된다.
    assert state.room in state.livekit.rooms


# ---- 웹훅 룸 스코프 ----


def test_webhook_ignores_intercom_room_events(state):
    """인터컴 룸 이벤트는 릴레이 강제(제거·strike·on-air)를 건드리지 않는다."""
    proc = WebhookProcessor(state)
    room = state.intercom_room
    ident = "intercom-aaaa1111"
    _run(proc.handle(_event("participant_joined", ident, "ic1", room)))
    _run(proc.handle(_event("track_published", ident, "ic2", room, track_name="ic-aaaa1111")))
    assert state.livekit.removed == []
    assert not state.blocklist.is_blocked(ident)
    # 멱등 커밋은 수행된다(재전송 루프 방지).
    assert state.db.is_webhook_processed("ic1")


def test_webhook_intercom_room_does_not_touch_relay_state(state):
    """인터컴 룸에서 릴레이 규약 identity·트랙명이 와도 릴레이 상태를 오염시키지 않는다."""
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    fake_speaker = "speaker-ch-01-e1-g1-nFAKE01"
    _run(
        proc.handle(
            _event("track_published", fake_speaker, "ic3", state.intercom_room, track_name="ch-01")
        )
    )
    assert state.on_air.is_on_air(1) is False
    assert state.livekit.removed == []


def test_webhook_relay_room_still_enforced(state):
    """룸 스코프 가드가 릴레이 룸 강제를 약화시키지 않는다(기존 규칙 회귀)."""
    proc = WebhookProcessor(state)
    bad = "intruder-1"
    _run(proc.handle(_event("participant_joined", bad, "rl1", state.room)))
    assert (state.room, bad) in state.livekit.removed
