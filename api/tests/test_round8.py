# codex 8차 잔여 결함 검증 — 웹훅 처리성공 후 커밋(#1)·채널락(#2)·루프백 http(#4)·stale unpublish(#5)·joined lease 고착(#6)
from __future__ import annotations

import asyncio

from livekit.api import WebhookEvent
from livekit.protocol.models import ParticipantInfo, Room, TrackInfo

from app.config import PUBLISH_TTL_SECONDS
from app.db import Database, new_nonce
from app.main import _https_guard, _is_loopback
from app.state import AppState
from app.webhook import WebhookProcessor
from tests.conftest import MockLiveKit, make_settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _event(kind, identity, event_id, room="field-g1", track_name=None):
    ev = WebhookEvent(event=kind, id=event_id)
    ev.room.CopyFrom(Room(name=room))
    ev.participant.CopyFrom(ParticipantInfo(identity=identity))
    if track_name is not None:
        ev.track.CopyFrom(TrackInfo(name=track_name))
    return ev


def _lease_ident(state, cid=1):
    _ok, ident = state.db.acquire_lease(cid, 1, state.generation, new_nonce(), PUBLISH_TTL_SECONDS)
    return ident


class _P:
    def __init__(self, identity, ntracks=1):
        self.identity = identity
        self.tracks = list(range(ntracks))


# ---- #1 웹훅: RemoveParticipant 실패 시 503 유도 + id 미커밋 + 재전송 시 재처리 ----
class _FailingRemoveLK(MockLiveKit):
    """remove_participant 가 항상 (not-found 아닌) 실패를 던지는 mock."""

    def __init__(self) -> None:
        super().__init__()
        self.remove_attempts = 0

    async def remove_participant(self, room: str, identity: str) -> None:
        self.remove_attempts += 1
        raise RuntimeError("livekit connection refused")


def test_webhook_remove_failure_propagates_and_not_committed(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = _FailingRemoveLK()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    proc = WebhookProcessor(st)
    st.db.create_channel(1, "ko", "한국어")
    _lease_ident(st)

    # 구 generation identity → 제거 대상. 제거가 실패하므로 handle 은 예외를 전파해야 한다.
    bad = "speaker-ch-01-e1-g99-nFAIL01"
    ev = _event("participant_joined", bad, "evt-fail-1")
    raised = False
    try:
        _run(proc.handle(ev))
    except Exception:
        raised = True
    assert raised is True, "RemoveParticipant 실패 시 handle 이 예외를 전파해야 한다"
    # id 는 커밋되지 않아야 한다(재전송 시 재처리).
    assert st.db.is_webhook_processed("evt-fail-1") is False
    n1 = lk.remove_attempts

    # 재전송 → 다시 제거를 시도(재처리)해야 한다.
    try:
        _run(proc.handle(ev))
    except Exception:
        pass
    assert lk.remove_attempts == n1 + 1, "재전송 시 다시 제거를 시도(재처리)해야 한다"
    db.close()


def test_webhook_commits_only_after_success(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_ident(state)
    # 정상 joined(제거 없음) → 처리 성공 → id 커밋됨.
    _run(proc.handle(_event("participant_joined", ident, "evt-ok-1")))
    assert state.db.is_webhook_processed("evt-ok-1") is True


# ---- #2 채널 락: close 진행 중 publish 발급이 끼어들지 못한다 ----
# TestClient 는 자체 이벤트루프를 쓰므로 cross-loop 프리미티브 문제를 피하기 위해
# 실제 라우트 대신 라우트가 쓰는 것과 동일한 채널 락으로 임계 구역 순서를 검증한다.
def test_channel_lock_serializes_close_and_publish(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path)
        db = Database(settings.db_path)
        lk = MockLiveKit()
        st = AppState(settings, db, lk)
        await st.bootstrap()
        st.db.create_channel(1, "ko", "한국어")
        ident = _lease_ident(st)
        st.db.mark_lease_joined(1, ident)
        lk.participants[st.room] = [_P(ident, 1)]

        gate = asyncio.Event()
        order: list[str] = []

        async def close_like():
            # main.close_channel 임계 구역 모사: 채널 락 안에서 remove(await) 후 상태 전이.
            async with st.channel_lock(1):
                order.append("close-enter")
                await gate.wait()  # RemoveParticipant 지연으로 경쟁 창을 연다.
                await lk.remove_participant(st.room, ident)
                st.db.close_channel(1)
                order.append("close-exit")

        async def publish_like():
            # main.issue_publish 임계 구역 모사: 채널 락 안에서만 lease 획득 가능.
            async with st.channel_lock(1):
                order.append("publish-enter")
                ch = st.db.get_channel(1)
                # 락이 직렬화하면 이 시점에 채널은 이미 closed 여야 한다.
                return "acquired" if (ch and ch.state == "open") else "channel_closed"

        close_task = asyncio.ensure_future(close_like())
        await asyncio.sleep(0.02)  # close 가 먼저 락을 잡게 한다.
        publish_task = asyncio.ensure_future(publish_like())
        await asyncio.sleep(0.02)  # publish 는 락 대기(끼어들지 못함).
        gate.set()
        await close_task
        result = await publish_task
        db.close()
        return order, result

    order, result = _run(scenario())
    # publish 임계 구역은 close 가 끝난 뒤에야 진입해야 한다(직렬화).
    assert order.index("close-exit") < order.index("publish-enter")
    assert result == "channel_closed", "close 도중 발급이 끼어들면 안 된다(락 뒤에서 종료 관측)"


# ---- #4 루프백 http 인증 허용 ----
class _Req:
    def __init__(self, host, headers=None, scheme="http"):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()


def test_is_loopback():
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("::1") is True
    assert _is_loopback("192.168.0.10") is False
    assert _is_loopback("not-an-ip") is False


def test_https_guard_allows_loopback_http(state):
    # 서버 자신(127.0.0.1)이 http 로 인증 요청 → 루프백 예외로 허용(None).
    req = _Req("127.0.0.1", scheme="http")
    assert _https_guard(req, state) is None


def test_https_guard_allows_loopback_via_trusted_xff(state):
    # Caddy(신뢰 프록시) 경유로 XFF 가 127.0.0.1 → 루프백 예외 허용.
    req = _Req("172.18.0.5", headers={"x-forwarded-for": "127.0.0.1"}, scheme="http")
    assert _https_guard(req, state) is None


def test_https_guard_rejects_external_http(state):
    # 외부(비루프백) http 인증 요청은 403 유지.
    req = _Req("203.0.113.9", scheme="http")
    resp = _https_guard(req, state)
    assert resp is not None
    assert resp.status_code == 403


# ---- #5 stale track_unpublished 가 새 on-air 를 지우지 못한다 ----
def test_stale_track_unpublished_ignored(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ident = _lease_ident(state)
    # 현재 송신자가 발행 중(on-air).
    _run(proc.handle(_event("track_published", ident, "tp1", track_name="ch-01")))
    assert state.on_air.is_on_air(1) is True
    # 구 송신자(다른 nonce)의 지연 track_unpublished → 무시(on-air 유지).
    stale = "speaker-ch-01-e1-g1-nSTALE9"
    assert stale != ident
    _run(proc.handle(_event("track_unpublished", stale, "tu1")))
    assert state.on_air.is_on_air(1) is True, "stale unpublish 가 현재 on-air 를 지우면 안 된다"
    # 현재 lease identity 의 unpublish → 정상 해제.
    _run(proc.handle(_event("track_unpublished", ident, "tu2")))
    assert state.on_air.is_on_air(1) is False


# ---- #6① reconcile: 룸에 없는 joined lease 해제 ----
def test_reconcile_releases_absent_joined_lease(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    st.db.create_channel(1, "ko", "한국어")
    ident = _lease_ident(st)
    st.db.mark_lease_joined(1, ident)  # joined 상태.
    # 룸에는 이 participant 가 실존하지 않음(LiveKit 재시작 가정) → 재-bootstrap.
    lk.participants[st.room] = []
    _run(st.bootstrap())
    assert st.db.get_lease(1) is None, "룸에 없는 joined lease 는 reconcile 에서 해제돼야 한다"
    db.close()


# ---- #6② publish: joined 이지만 룸 부재면 재발급 허용(409 아님) ----
def _make_state_with_joined_lease(tmp_path, present: bool):
    """joined lease 를 잡고 룸 실존 여부(present)를 mock 한 state 를 반환한다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    st.db.create_channel(1, "ko", "한국어")
    ident = _lease_ident(st)
    st.db.mark_lease_joined(1, ident)
    lk.participants[st.room] = [_P(ident, 1)] if present else []
    return settings, db, st


def test_publish_reissues_when_joined_lease_absent(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings, db, st = _make_state_with_joined_lease(tmp_path, present=False)
    app = create_app(state=st)
    with TestClient(app, base_url="https://testserver") as client:
        headers = {"Authorization": f"Bearer {settings.send_password}"}
        r = client.post("/publish-tokens", json={"channel_id": 1}, headers=headers)
    db.close()
    assert r.status_code == 200, "룸에 없는 고착 joined lease 는 새 발급을 막으면 안 된다"


def test_publish_still_409_when_joined_lease_present(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings, db, st = _make_state_with_joined_lease(tmp_path, present=True)
    app = create_app(state=st)
    with TestClient(app, base_url="https://testserver") as client:
        headers = {"Authorization": f"Bearer {settings.send_password}"}
        r = client.post("/publish-tokens", json={"channel_id": 1}, headers=headers)
    db.close()
    assert r.status_code == 409, "룸에 실존하는 활성 송신 lease 는 409(만료 덮어쓰기 금지) 유지"


# ==== codex 9차 결함 #2: reconcile fail-fast + 조회 실패 시 409 유지 ====


class _ListPartFailLK(MockLiveKit):
    """list_participants 가 항상 (조회) 실패를 던지는 mock."""

    async def list_participants(self, room: str) -> list:
        raise RuntimeError("livekit list_participants connection refused")


class _RemoveFailLK(MockLiveKit):
    """remove_participant 만 (not-found 아닌) 실패를 던지는 mock."""

    async def remove_participant(self, room: str, identity: str) -> None:
        raise RuntimeError("livekit remove connection refused")


def test_reconcile_fails_fast_on_list_participants_failure(tmp_path):
    """#2①: ListParticipants 조회 실패는 빈 룸으로 간주하지 않고 기동을 실패시킨다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = _ListPartFailLK()
    st = AppState(settings, db, lk)
    raised = False
    try:
        _run(st.bootstrap())
    except Exception:
        raised = True
    db.close()
    assert raised, "reconcile 조회 실패는 예외를 전파해 기동을 중단해야 한다(빈 룸 간주 금지)"


def test_reconcile_fails_fast_on_orphan_remove_failure(tmp_path):
    """#2②: 고아 발행자 RemoveParticipant 실패도 삼키지 않고 기동을 실패시킨다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = _RemoveFailLK()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())  # 첫 기동은 참가자 없음 → 정상.
    st.db.create_channel(1, "ko", "한국어")
    # lease 없는 고아 발행자를 룸에 심는다 → 재-bootstrap 시 제거 시도 → 제거 실패.
    lk.participants[st.room] = [_P("speaker-ch-01-e1-g1-nORPHAN", 1)]
    raised = False
    try:
        _run(st.bootstrap())
    except Exception:
        raised = True
    db.close()
    assert raised, "고아 발행자 제거 실패는 예외를 전파해 기동을 중단해야 한다"


def test_publish_keeps_409_when_present_check_fails(tmp_path):
    """#2③: joined lease 존재 확인 조회가 실패하면 lease 를 해제하지 않고 409 를 유지한다."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    st.db.create_channel(1, "ko", "한국어")
    ident = _lease_ident(st)
    st.db.mark_lease_joined(1, ident)

    # 이후 list_participants(존재 확인)만 실패하도록 교체한다(기동은 이미 끝남).
    async def _boom(room: str) -> list:
        raise RuntimeError("livekit list_participants connection refused")

    lk.list_participants = _boom

    app = create_app(state=st)
    with TestClient(app, base_url="https://testserver") as client:
        headers = {"Authorization": f"Bearer {settings.send_password}"}
        r = client.post("/publish-tokens", json={"channel_id": 1}, headers=headers)
    # lease 가 해제되지 않았는지 확인(보수적으로 존재 간주).
    lease_after = st.db.get_lease(1)
    db.close()
    assert r.status_code == 409, "존재 확인 조회 실패 시 활성 송신을 끊지 말고 409 를 유지해야 한다"
    assert lease_after is not None and lease_after.identity == ident, "조회 실패 시 lease 를 해제하면 안 된다"


# ==== codex 10차 결함 #1: bootstrap ListRooms 조회 실패도 fail-fast ====


class _ListRoomsFailLK(MockLiveKit):
    """list_rooms 가 항상 (조회) 실패를 던지는 mock."""

    async def list_rooms(self) -> list[str]:
        raise RuntimeError("livekit list_rooms connection refused")


def test_bootstrap_fails_fast_on_list_rooms_failure(tmp_path):
    """#1: ListRooms 조회 실패를 빈 목록으로 간주하면 구세대 룸 삭제를 건너뛰어
    폐기된 세대 JWT 가 계속 유효할 수 있다. 조회 실패는 기동을 실패시켜야 한다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = _ListRoomsFailLK()
    st = AppState(settings, db, lk)
    raised = False
    try:
        _run(st.bootstrap())
    except Exception:
        raised = True
    db.close()
    assert raised, "bootstrap ListRooms 조회 실패는 예외를 전파해 기동을 중단해야 한다"
