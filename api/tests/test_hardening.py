# codex 7차 결함 수정 검증 — https 강제·forwarded IP·ensure_room·웹훅 재시도·재검증·grace·채널대조·422
from __future__ import annotations

import asyncio

from livekit.api import WebhookEvent
from livekit.protocol.models import ParticipantInfo, Room, TrackInfo

from app.config import LEASE_JOIN_GRACE_SECONDS, PUBLISH_TTL_SECONDS
from app.db import Database, new_nonce
from app.http_util import client_ip, is_https
from app.webhook import WebhookProcessor


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _event(kind, identity, event_id, room="field-g1", track_name=None):
    ev = WebhookEvent(event=kind, id=event_id)
    ev.room.CopyFrom(Room(name=room))
    ev.participant.CopyFrom(ParticipantInfo(identity=identity))
    if track_name is not None:
        ev.track.CopyFrom(TrackInfo(name=track_name))
    return ev


# ---- #1 https 강제 ----
def test_publish_over_http_rejected_403(http_client, send_headers):
    # 먼저 채널 개설도 http 면 403.
    r = http_client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    assert r.status_code == 403
    assert r.json()["code"] == "https_required"


def test_publish_tokens_over_http_rejected(http_client, client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    r = http_client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r.status_code == 403
    assert r.json()["code"] == "https_required"


def test_admin_over_http_rejected(http_client, admin_headers):
    r = http_client.get("/admin/status", headers=admin_headers)
    assert r.status_code == 403


def test_unauthed_reads_allowed_over_http(http_client, client, send_headers):
    # 무인증 읽기(GET /channels, subscribe-tokens, heartbeat, status, healthz)는 http 허용.
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    assert http_client.get("/channels").status_code == 200
    assert http_client.get("/status").status_code == 200
    assert http_client.get("/healthz").status_code == 200
    assert http_client.post("/channels/1/subscribe-tokens").status_code == 200


# ---- #2 forwarded IP 기반 rate limit ----
def test_client_ip_prefers_forwarded_when_peer_trusted():
    # peer(직접 접속)가 신뢰 프록시면 XFF 의 최초 클라이언트 IP 를 채택.
    class _Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    allow = "172.16.0.0/12"
    req = _Req("172.18.0.5", {"x-forwarded-for": "203.0.113.7, 172.18.0.5"})
    assert client_ip(req, allow) == "203.0.113.7"


def test_client_ip_ignores_forwarded_when_peer_untrusted():
    class _Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    allow = "172.16.0.0/12"
    # peer 가 비신뢰면 XFF 무시(스푸핑 방지).
    req = _Req("8.8.8.8", {"x-forwarded-for": "1.2.3.4"})
    assert client_ip(req, allow) == "8.8.8.8"


def test_is_https_via_forwarded_proto():
    class _Req:
        def __init__(self, host, headers, scheme="http"):
            self.client = type("C", (), {"host": host})()
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    allow = "172.16.0.0/12"
    assert is_https(_Req("172.18.0.5", {"x-forwarded-proto": "https"}), allow) is True
    assert is_https(_Req("172.18.0.5", {"x-forwarded-proto": "http"}), allow) is False
    # 프록시 아니면 헤더 무시하고 url.scheme.
    assert is_https(_Req("8.8.8.8", {"x-forwarded-proto": "https"}, scheme="http"), allow) is False


def test_rate_limit_uses_forwarded_ip(client, send_headers):
    # 서로 다른 XFF IP 는 rate limit 키가 분리되어야 한다(Caddy IP 로 묶이지 않음).
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    # 신뢰 peer(testclient 는 비신뢰) → XFF 를 신뢰하려면 신뢰 대역에서 와야 한다.
    # TestClient peer 는 'testclient' 라 비신뢰이므로 XFF 무시. 대신 http_util 단위 테스트로 커버.
    # 여기서는 subscribe rate limit 이 정상 동작하는지만 확인.
    codes = [client.post("/channels/1/subscribe-tokens").status_code for _ in range(12)]
    assert 429 in codes


# ---- #3 ensure_room ----
def test_ensure_room_recreates_after_delete(state):
    # 룸 삭제 후 subscribe 토큰 발급 시 재생성.
    _run(state.livekit.delete_room(state.room))
    assert state.room not in state.livekit.rooms
    _run(state.ensure_room())
    assert state.room in state.livekit.rooms


def test_publish_recreates_room(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    st = client.app.state.field
    _run(st.livekit.delete_room(st.room))
    r = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r.status_code == 200
    assert st.room in st.livekit.rooms


# ---- #4 웹훅 처리 실패 재시도 ----
def test_webhook_processing_failure_returns_5xx_and_no_commit(client, send_headers, monkeypatch):
    # 서명 검증을 통과시키고 handle 이 예외를 던지게 해 503 확인.
    st = client.app.state.field
    proc = client.app.state.webhook

    class _Ev:
        id = "evt-fail-1"
        event = "participant_joined"

    monkeypatch.setattr(proc, "verify", lambda body, auth: _Ev())

    async def _boom(event):
        raise RuntimeError("처리 실패")

    monkeypatch.setattr(proc, "handle", _boom)
    r = client.post("/livekit/webhook", content=b"{}", headers={"Authorization": "x"})
    assert r.status_code == 503
    # event id 는 커밋되지 않아야 한다(재시도 가능).
    assert st.db.mark_webhook_processed("evt-fail-1") is True


def test_webhook_commits_only_after_success(state):
    # handle 성공 후에만 event id 커밋(멱등).
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    ev = _event("participant_joined", "monitor-x", "evt-ok-1")
    _run(proc.handle(ev))
    # 커밋됐으므로 재기록은 False.
    assert state.db.mark_webhook_processed("evt-ok-1") is False


# ---- #5 track_published lease 재검증 ----
def test_track_published_revalidates_lease(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _ok, ident = state.db.acquire_lease(1, 1, state.generation, new_nonce(), PUBLISH_TTL_SECONDS)
    # 현재 lease 와 다른 identity 로 트랙 발행(같은 채널·규약이지만 nonce 상이) → 제거.
    other = "speaker-ch-01-e1-g1-nZZZZZ9"
    assert other != ident
    _run(proc.handle(_event("track_published", other, "tp1", track_name="ch-01")))
    assert (state.room, other) in state.livekit.removed


def test_track_published_empty_name_rejected(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _ok, ident = state.db.acquire_lease(1, 1, state.generation, new_nonce(), PUBLISH_TTL_SECONDS)
    _run(proc.handle(_event("track_published", ident, "tp2", track_name="")))
    assert (state.room, ident) in state.livekit.removed


def test_track_published_closed_channel_rejected(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _ok, ident = state.db.acquire_lease(1, 1, state.generation, new_nonce(), PUBLISH_TTL_SECONDS)
    state.db.close_channel(1)  # lease 도 삭제됨
    _run(proc.handle(_event("track_published", ident, "tp3", track_name="ch-01")))
    assert (state.room, ident) in state.livekit.removed


# ---- #6 joined lease 연장/grace 해제 ----
def test_unjoined_lease_grace_release(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    # 발급 후 접속 실패(joined 안 됨). grace 를 넘긴 상태를 흉내: TTL 을 grace 보다 짧게.
    ok1, id1 = db.acquire_lease(1, 1, 1, "AAA111", ttl_seconds=1, grace_seconds=0)
    assert ok1 is True
    import time

    time.sleep(1.1)  # TTL 만료
    # grace 적용 재획득 — 미접속 + TTL 만료 → 재획득 성공.
    ok2, id2 = db.acquire_lease(1, 1, 1, "BBB222", PUBLISH_TTL_SECONDS, LEASE_JOIN_GRACE_SECONDS)
    assert ok2 is True
    assert id1 != id2
    db.close()


def test_unjoined_lease_within_grace_blocks(tmp_path):
    db = Database(str(tmp_path / "g2.db"))
    # 방금 발급된 미접속 lease 는 grace 안이라 재획득 거부.
    ok1, _ = db.acquire_lease(1, 1, 1, "AAA111", PUBLISH_TTL_SECONDS, LEASE_JOIN_GRACE_SECONDS)
    assert ok1 is True
    ok2, _ = db.acquire_lease(1, 1, 1, "BBB222", PUBLISH_TTL_SECONDS, LEASE_JOIN_GRACE_SECONDS)
    assert ok2 is False
    db.close()


def test_joined_lease_auto_extends_when_publisher_present(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    p = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers).json()
    ident = p["identity"]
    st = client.app.state.field
    # joined 마킹 + 만료시킴.
    st.db.mark_lease_joined(1, ident)
    # 강제로 TTL 만료 상태로 만든다.
    with st.db._tx():
        st.db._conn.execute("UPDATE leases SET expires_at=? WHERE channel_id=1", (0,))

    # participant 가 룸에 남아 있다고 mock.
    class _P:
        identity = ident
        tracks: list = []

    st.livekit.participants[st.room] = [_P()]
    # 새 발급 시도 → 활성 송신 보호로 409 + lease 연장.
    r = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r.status_code == 409
    lease = st.db.get_lease(1)
    assert lease.identity == ident  # 덮어써지지 않음
    assert lease.expires_at > 0  # 연장됨


# ---- #7 fail-open 방지 ----
def test_close_channel_502_on_remove_failure(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    st = client.app.state.field

    async def _fail(room, identity):
        raise RuntimeError("livekit down")

    st.livekit.remove_participant = _fail
    r = client.delete("/channels/1", headers=send_headers)
    assert r.status_code == 502
    assert r.json()["code"] == "livekit_error"
    # 상태 전이 안 됨: 채널 여전히 open, lease 유지.
    assert st.db.get_channel(1).state == "open"
    assert st.db.get_lease(1) is not None


def test_takeover_502_on_remove_failure(client, send_headers, admin_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    p1 = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers).json()
    st = client.app.state.field

    async def _fail(room, identity):
        raise RuntimeError("livekit down")

    st.livekit.remove_participant = _fail
    r = client.post("/admin/channels/1/takeover", headers=admin_headers)
    assert r.status_code == 502
    # epoch 안 오름.
    assert st.db.get_channel(1).epoch == 1
    assert st.db.get_lease(1).identity == p1["identity"]


# ---- #8 세대 무효화 실패 시 fail-fast ----
def test_bootstrap_fails_fast_on_room_delete_failure(tmp_path):
    from app.config import Settings
    from app.state import AppState
    from tests.conftest import MockLiveKit

    db_path = str(tmp_path / "ff.db")

    def build(send_pw, lk):
        settings = Settings(
            livekit_api_key="k", livekit_api_secret="s", livekit_host="http://livekit:7880",
            livekit_rtc_url="ws://livekit:7880",
            ws_url="ws://x:7880", send_password=send_pw, admin_password="admin-pw-1",
            db_path=db_path, recordings_path=str(tmp_path / "recordings"),
            max_channels=15, forwarded_allow_ips="127.0.0.1",
        )
        db = Database(db_path)
        st = AppState(settings, db, lk)
        return st, db

    # 1세대 기동.
    st1, db1 = build("pw-one", MockLiveKit())
    _run(st1.bootstrap())
    db1.close()

    # 2세대(비번 변경) — 구세대 룸 삭제가 실패하도록 mock → 기동 실패.
    lk = MockLiveKit()
    lk.rooms = ["field-g1"]

    async def _boom(name):
        raise RuntimeError("delete 실패")

    lk.delete_room = _boom
    st2, db2 = build("pw-two", lk)
    raised = False
    try:
        _run(st2.bootstrap())
    except RuntimeError:
        raised = True
    db2.close()
    assert raised is True


def test_generation_change_clears_leases(tmp_path):
    from app.config import Settings
    from app.state import AppState
    from tests.conftest import MockLiveKit

    db_path = str(tmp_path / "gl.db")

    def build(send_pw):
        settings = Settings(
            livekit_api_key="k", livekit_api_secret="s", livekit_host="http://livekit:7880",
            livekit_rtc_url="ws://livekit:7880",
            ws_url="ws://x:7880", send_password=send_pw, admin_password="admin-pw-1",
            db_path=db_path, recordings_path=str(tmp_path / "recordings"),
            max_channels=15, forwarded_allow_ips="127.0.0.1",
        )
        db = Database(db_path)
        st = AppState(settings, db, MockLiveKit())
        _run(st.bootstrap())
        return st, db

    st1, db1 = build("pw-one")
    db1.create_channel(1, "ko", "한국어")
    db1.acquire_lease(1, 1, st1.generation, "AAA111", PUBLISH_TTL_SECONDS)
    assert db1.get_lease(1) is not None
    db1.close()

    # 비번 변경 → 세대 +1 → 구세대 lease 전부 정리.
    st2, db2 = build("pw-two")
    assert st2.generation == 2
    assert db2.get_lease(1) is None
    db2.close()


# ---- #10 issued_listeners TTL 정리 ----
def test_issued_listener_ttl_purge(tmp_path):
    db = Database(str(tmp_path / "il.db"))
    db.create_channel(1, "ko", "한국어")
    lid = db.issue_listener(1)
    assert db.issued_listener_channel(lid) == 1
    # 발급 시각을 과거로 밀어 TTL 만료.
    with db._tx():
        db._conn.execute("UPDATE issued_listeners SET issued_at=? WHERE listener_id=?", (0, lid))
    # lazy 조회에서 만료 → None.
    assert db.issued_listener_channel(lid) is None
    db.purge_expired_issued_listeners()
    assert db.is_listener_issued(lid) is False
    # 근사 카운트도 재동기화(0).
    assert db.token_approximation_by_channel().get(1, 0) == 0
    db.close()


# ---- #11 heartbeat 채널 대조 ----
def test_heartbeat_channel_mismatch_ignored(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    client.post("/channels", json={"language": "en", "label": "English"}, headers=send_headers)
    tok = client.post("/channels/1/subscribe-tokens").json()
    lid = tok["listener_id"]
    # 발급은 채널 1, 보고는 채널 2 → 무시(204, 계수 안 됨).
    r = client.post("/listeners/heartbeat", json={"channel_id": 2, "listener_id": lid})
    assert r.status_code == 204
    status = client.get("/status").json()
    ch2 = next(c for c in status["channels"] if c["channel_id"] == 2)
    assert ch2["listeners"] == 0


def test_heartbeat_invalid_uuid_422(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    r = client.post("/listeners/heartbeat", json={"channel_id": 1, "listener_id": "not-a-uuid-xxxxxxxxxxxxxxxxxxxxxxxx"})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"


# ---- #12 heartbeat 미발급이면 limiter 키 생성 안 함 ----
def test_heartbeat_unissued_does_not_create_limiter_key(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    st = client.app.state.field
    fake = "00000000-0000-4000-8000-000000000000"
    for _ in range(20):
        assert client.post("/listeners/heartbeat", json={"channel_id": 1, "listener_id": fake}).status_code == 204
    # limiter 원장에 fake 키가 생성되지 않았어야 한다.
    assert fake not in st.heartbeat_rl._hits


# ---- #13 admin rate limit ----
def test_admin_status_rate_limited(client, admin_headers):
    codes = [client.get("/admin/status", headers=admin_headers).status_code for _ in range(35)]
    assert 429 in codes


def test_create_channel_rate_limited(client, send_headers):
    codes = []
    for _ in range(25):
        codes.append(client.post("/channels", json={"language": "ko", "label": "x"}, headers=send_headers).status_code)
    assert 429 in codes


# ---- #14 secrets.compare_digest 경로(잘못된 비번은 401) ----
def test_wrong_password_401(client):
    client_bad = {"Authorization": "Bearer wrong"}
    r = client.post("/channels", json={"language": "ko", "label": "x"}, headers=client_bad)
    assert r.status_code == 401


# ---- #15 422 Error 스키마 ----
def test_invalid_language_422_error_schema(client, send_headers):
    r = client.post("/channels", json={"language": "!!bad", "label": "x"}, headers=send_headers)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "message" in body


# ---- #16 track_unpublished on-air 해제 ----
def test_track_unpublished_clears_on_air(state):
    proc = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    _ok, ident = state.db.acquire_lease(1, 1, state.generation, new_nonce(), PUBLISH_TTL_SECONDS)
    _run(proc.handle(_event("track_published", ident, "u1", track_name="ch-01")))
    assert state.on_air.is_on_air(1) is True
    _run(proc.handle(_event("track_unpublished", ident, "u2", track_name="ch-01")))
    assert state.on_air.is_on_air(1) is False
