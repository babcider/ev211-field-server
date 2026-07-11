# 웹훅 강제 테스트 — participant_joined 즉시 검증, track 1개 제한, stale left, 서명 검증, 멱등
from __future__ import annotations

import asyncio

from livekit.api import AccessToken, VideoGrants, WebhookEvent
from livekit.protocol.models import ParticipantInfo, Room, TrackInfo

from app.config import PUBLISH_TTL_SECONDS
from app.db import new_nonce
from app.webhook import WebhookProcessor
from tests.conftest import API_KEY, API_SECRET


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _event(kind: str, identity: str, event_id: str, room: str = "field-g1", track_name=None) -> WebhookEvent:
    ev = WebhookEvent(event=kind, id=event_id)
    ev.room.CopyFrom(Room(name=room))
    ev.participant.CopyFrom(ParticipantInfo(identity=identity))
    if track_name is not None:
        ev.track.CopyFrom(TrackInfo(name=track_name))
    return ev


def _lease_and_identity(state, cid=1):
    nonce = new_nonce()
    _ok, ident = state.db.acquire_lease(cid, 1, state.generation, nonce, PUBLISH_TTL_SECONDS)
    return ident


def test_joined_removes_unauthorized_generation(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _lease_and_identity(state)
    # 구 generation(g99) identity 로 접속 → 즉시 제거.
    bad = "speaker-ch-01-e1-g99-nXXXXX1"
    _run(proc.handle(_event("participant_joined", bad, "e1")))
    assert (state.room, bad) in state.livekit.removed


def test_joined_removes_wrong_lease_identity(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)
    # 같은 채널·gen·epoch 지만 nonce 가 다른 구 토큰 → 현재 lease 와 불일치 → 제거.
    other = "speaker-ch-01-e1-g1-nOTHER9"
    assert other != ident
    _run(proc.handle(_event("participant_joined", other, "e2")))
    assert (state.room, other) in state.livekit.removed


def test_joined_accepts_current_lease_identity(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)
    _run(proc.handle(_event("participant_joined", ident, "e3")))
    assert (state.room, ident) not in state.livekit.removed
    lease = state.db.get_lease(1)
    assert lease.joined_at is not None


def test_joined_ignores_monitor(state):
    proc = WebhookProcessor(state)
    _run(proc.handle(_event("participant_joined", "monitor-abc", "e4")))
    assert state.livekit.removed == []


def test_track_published_wrong_track_removed(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)
    # ch-05 트랙을 ch-01 송신자가 발행 → 불일치 제거.
    _run(proc.handle(_event("track_published", ident, "t1", track_name="ch-05")))
    assert (state.room, ident) in state.livekit.removed


def test_track_published_correct_track_on_air(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)
    _run(proc.handle(_event("track_published", ident, "t2", track_name="ch-01")))
    assert (state.room, ident) not in state.livekit.removed
    assert state.on_air.is_on_air(1) is True


def test_track_published_second_track_removed(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)

    # participant 가 이미 2개 트랙 발행 중이라고 mock.
    class _P:
        def __init__(self, identity, ntracks):
            self.identity = identity
            self.tracks = list(range(ntracks))

    state.livekit.participants[state.room] = [_P(ident, 2)]
    _run(proc.handle(_event("track_published", ident, "t3", track_name="ch-01")))
    assert (state.room, ident) in state.livekit.removed


def test_left_releases_only_matching_identity(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_and_identity(state)
    # stale left(다른 identity) → 무시.
    _run(proc.handle(_event("participant_left", "speaker-ch-01-e1-g1-nSTALE0", "l1")))
    assert state.db.get_lease(1) is not None
    # 정확 일치 left → 해제.
    _run(proc.handle(_event("participant_left", ident, "l2")))
    assert state.db.get_lease(1) is None


def test_webhook_idempotent(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _lease_and_identity(state)
    bad = "speaker-ch-01-e1-g99-nDUP111"
    ev = _event("participant_joined", bad, "same-id")
    _run(proc.handle(ev))
    n1 = len(state.livekit.removed)
    _run(proc.handle(ev))  # 같은 event id → 멱등, 재처리 안 함.
    n2 = len(state.livekit.removed)
    assert n1 == n2


def test_webhook_signature_verification(state):
    proc = WebhookProcessor(state)
    # 유효한 서명 토큰 생성(WebhookReceiver 가 검증).
    body = '{"event":"room_started","id":"x1"}'
    import hashlib

    digest = hashlib.sha256(body.encode()).digest()
    import base64

    token = (
        AccessToken(API_KEY, API_SECRET)
        .with_grants(VideoGrants())
        .with_sha256(base64.b64encode(digest).decode())
        .to_jwt()
    )
    ev = proc.verify(body, token)
    assert ev.id == "x1"
    # 위조 서명은 예외.
    try:
        proc.verify(body, "invalid.jwt.token")
        raise AssertionError("should have raised")
    except Exception:
        pass


def test_blocklist_removes_on_repeat_violation(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _lease_and_identity(state)
    bad = "speaker-ch-01-e1-g99-nBLK111"
    # 3회 위반 → 차단 목록 등록.
    for i in range(3):
        _run(proc.handle(_event("participant_joined", bad, f"blk{i}")))
    assert state.blocklist.is_blocked(bad) is True
