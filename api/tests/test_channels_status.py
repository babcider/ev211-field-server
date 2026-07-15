# 채널 CRUD·상태·heartbeat 집계·rate limit·세대 증가 테스트
from __future__ import annotations

from app.state import password_hash  # noqa: F401


def _open(client, headers, **kw):
    body = {"language": "ko", "label": "한국어"}
    body.update(kw)
    return client.post("/channels", json=body, headers=headers)


def test_create_requires_password(client):
    r = client.post("/channels", json={"language": "ko", "label": "한국어"})
    assert r.status_code == 401


def test_create_auto_slot_lowest(client, send_headers):
    r1 = _open(client, send_headers)
    assert r1.status_code == 201
    assert r1.json()["channel_id"] == 1
    r2 = _open(client, send_headers)
    assert r2.json()["channel_id"] == 2


def test_create_floor_channel_0(client, send_headers):
    r = _open(client, send_headers, channel_id=0)
    assert r.status_code == 201
    assert r.json()["channel_id"] == 0
    assert r.json()["track_name"] == "ch-00"


def test_max_channels_reached(client, send_headers):
    for i in range(15):
        assert _open(client, send_headers).status_code == 201
    r = _open(client, send_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "max_channels_reached"
    # 채널 0 Floor 는 한도와 무관하게 개설 가능.
    r0 = _open(client, send_headers, channel_id=0)
    assert r0.status_code == 201


def test_invalid_language_422(client, send_headers):
    r = client.post("/channels", json={"language": "not_a_lang!", "label": "x"}, headers=send_headers)
    assert r.status_code == 422


def test_list_channels_shape(client, send_headers):
    _open(client, send_headers)
    r = client.get("/channels")
    assert r.status_code == 200
    body = r.json()
    assert body["generation"] == 1
    assert body["max_channels"] == 15
    assert body["channels"][0]["track_name"] == "ch-01"


def test_heartbeat_aggregation(client, send_headers):
    _open(client, send_headers)
    tok = client.post("/channels/1/subscribe-tokens").json()
    lid = tok["listener_id"]
    r = client.post("/listeners/heartbeat", json={"channel_id": 1, "listener_id": lid})
    assert r.status_code == 204
    status = client.get("/status").json()
    ch1 = next(c for c in status["channels"] if c["channel_id"] == 1)
    assert ch1["listeners"] == 1
    assert status["listeners_source"] == "heartbeat"


def test_heartbeat_keeps_long_lived_listener_counted(client, send_headers, state):
    """1시간 넘게 연속 heartbeat 하는 청취자는 발급 원장 만료로 사라지지 않아야 한다."""
    import time

    from app.config import ISSUED_LISTENER_TTL_SECONDS

    _open(client, send_headers)
    tok = client.post("/channels/1/subscribe-tokens").json()
    lid = tok["listener_id"]
    # 발급 시각을 TTL 보다 오래 전으로 되돌려 "오래 접속" 상태를 흉내.
    old = time.time() - ISSUED_LISTENER_TTL_SECONDS - 10
    with state.db._tx():  # noqa: SLF001
        state.db._conn.execute(  # noqa: SLF001
            "UPDATE issued_listeners SET issued_at=? WHERE listener_id=?", (old, lid)
        )
    # heartbeat 가 타임스탬프를 갱신하므로 여전히 인정되어야 한다.
    r = client.post("/listeners/heartbeat", json={"channel_id": 1, "listener_id": lid})
    assert r.status_code == 204
    status = client.get("/status").json()
    ch1 = next(c for c in status["channels"] if c["channel_id"] == 1)
    assert ch1["listeners"] == 1
    assert status["listeners_source"] == "heartbeat"


def test_heartbeat_channel_mismatch_ignored(client, send_headers):
    """발급 채널과 다른 채널로 온 heartbeat 는 무시된다(계수 조작 방지)."""
    _open(client, send_headers)
    _open(client, send_headers, channel=2)
    tok = client.post("/channels/1/subscribe-tokens").json()
    r = client.post("/listeners/heartbeat", json={"channel_id": 2, "listener_id": tok["listener_id"]})
    assert r.status_code == 204
    status = client.get("/status").json()
    ch2 = next(c for c in status["channels"] if c["channel_id"] == 2)
    assert ch2["listeners"] == 0


def test_heartbeat_unknown_listener_ignored(client, send_headers):
    _open(client, send_headers)
    fake = "00000000-0000-4000-8000-000000000000"
    r = client.post("/listeners/heartbeat", json={"channel_id": 1, "listener_id": fake})
    assert r.status_code == 204  # 조용히 무시
    status = client.get("/status").json()
    ch1 = next(c for c in status["channels"] if c["channel_id"] == 1)
    assert ch1["listeners"] == 0


def test_token_approximation_fallback(client, send_headers):
    _open(client, send_headers)
    # heartbeat 없이 subscribe 토큰만 2회 발급.
    client.post("/channels/1/subscribe-tokens")
    client.post("/channels/1/subscribe-tokens")
    status = client.get("/status").json()
    assert status["listeners_source"] == "token_approximation"
    ch1 = next(c for c in status["channels"] if c["channel_id"] == 1)
    assert ch1["listeners"] == 2


def test_subscribe_rate_limit(client, send_headers):
    _open(client, send_headers)
    codes = [client.post("/channels/1/subscribe-tokens").status_code for _ in range(12)]
    assert 429 in codes
    # 10회까지는 200.
    assert codes[:10] == [200] * 10


def test_publish_failure_lockout(client, send_headers):
    _open(client, send_headers)
    bad = {"Authorization": "Bearer wrong-password"}
    codes = []
    for _ in range(6):
        codes.append(client.post("/publish-tokens", json={"channel_id": 1}, headers=bad).status_code)
    # 5회 실패 후 잠금(423).
    assert 423 in codes


def test_generation_increment_on_password_change(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.state import AppState
    from tests.conftest import MockLiveKit
    import asyncio

    db_path = str(tmp_path / "gen.db")

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
        asyncio.get_event_loop().run_until_complete(st.bootstrap())
        return st, db

    st1, db1 = build("pw-one")
    assert st1.generation == 1
    db1.close()

    # 같은 비밀번호 → 세대 유지.
    st2, db2 = build("pw-one")
    assert st2.generation == 1
    db2.close()

    # 비밀번호 변경 → 세대 +1.
    st3, db3 = build("pw-two")
    assert st3.generation == 2
    db3.close()


def test_generation_bootstrap_creates_and_deletes_rooms(state):
    # bootstrap 후 새 세대 룸이 존재.
    assert state.room in state.livekit.rooms


def test_takeover_bumps_epoch(client, send_headers, admin_headers):
    _open(client, send_headers)
    p1 = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers).json()
    assert p1["epoch"] == 1
    # 정상은 409(busy). takeover 로 인수.
    r = client.post("/admin/channels/1/takeover", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["epoch"] == 2
    assert body["identity"].startswith("speaker-ch-01-e2-")


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["generation"] == 1
