# 무전기 채널 테스트 — 목록·개설(비번 옵션)·입장(채널 비번 검증)·상한·세대회전 정리
from __future__ import annotations

import asyncio

import jwt

from app.config import INTERCOM_MAX_CHANNELS
from app.tokens import intercom_channel_room_name
from tests.conftest import API_SECRET


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


# ---- 인증 ----


def test_channels_require_send_password(client, admin_headers):
    assert client.get("/intercom/channels").status_code == 401
    # 관리자 비번으로는 불가(송신자 전용).
    assert client.get("/intercom/channels", headers=admin_headers).status_code == 401


def test_channels_https_required(http_client, send_headers):
    r = http_client.get(
        "/intercom/channels", headers={**send_headers, "X-Forwarded-For": "192.168.0.9"}
    )
    assert r.status_code == 403


# ---- 개설 ----


def test_create_channel_assigns_lowest_slot(client, send_headers, state):
    r = client.post("/intercom/channels", json={"channel_name": "본부"}, headers=send_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["channel_id"] == 0
    assert body["name"] == "본부"
    assert body["has_password"] is False
    # 채널 룸이 max_participants 상한과 함께 생성됐다.
    room = state.intercom_channel_room(0)
    assert state.livekit.room_caps.get(room) == 10
    # 두 번째 채널은 슬롯 1.
    r2 = client.post("/intercom/channels", json={"channel_name": "무대"}, headers=send_headers)
    assert r2.json()["channel_id"] == 1


def test_create_channel_with_password(client, send_headers):
    r = client.post(
        "/intercom/channels",
        json={"channel_name": "비밀", "password": "secret1"},
        headers=send_headers,
    )
    assert r.status_code == 201
    assert r.json()["has_password"] is True


def test_create_channel_rejects_blank_name(client, send_headers):
    r = client.post("/intercom/channels", json={"channel_name": "   "}, headers=send_headers)
    assert r.status_code == 422


def test_create_channel_short_password_rejected(client, send_headers):
    r = client.post(
        "/intercom/channels",
        json={"channel_name": "x", "password": "12"},
        headers=send_headers,
    )
    assert r.status_code == 422


def test_channel_limit_reached(client, send_headers):
    for i in range(INTERCOM_MAX_CHANNELS):
        assert client.post(
            "/intercom/channels", json={"channel_name": f"c{i}"}, headers=send_headers
        ).status_code == 201
    over = client.post("/intercom/channels", json={"channel_name": "over"}, headers=send_headers)
    assert over.status_code == 409
    assert over.json()["code"] == "max_channels_reached"


def test_list_channels(client, send_headers):
    client.post("/intercom/channels", json={"channel_name": "A"}, headers=send_headers)
    client.post(
        "/intercom/channels", json={"channel_name": "B", "password": "pass12"}, headers=send_headers
    )
    r = client.get("/intercom/channels", headers=send_headers)
    assert r.status_code == 200
    chans = r.json()["channels"]
    assert [c["name"] for c in chans] == ["A", "B"]
    assert chans[0]["has_password"] is False
    assert chans[1]["has_password"] is True


# ---- 입장 ----


def test_enter_free_channel_issues_token(client, send_headers, state):
    client.post("/intercom/channels", json={"channel_name": "자유"}, headers=send_headers)
    r = client.post("/intercom/channels/0/tokens", headers=send_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["channel_id"] == 0
    assert body["room"] == intercom_channel_room_name(state.generation, 0)
    assert body["identity"].startswith("intercom-")
    assert body["track_name"].startswith("ic-")
    assert _decode(body["token"])["video"]["room"] == body["room"]


def test_enter_password_channel_requires_password(client, send_headers):
    client.post(
        "/intercom/channels",
        json={"channel_name": "잠금", "password": "open123"},
        headers=send_headers,
    )
    # 비번 없이 입장 → 401.
    assert client.post("/intercom/channels/0/tokens", headers=send_headers).status_code == 401
    # 틀린 비번 → 401.
    bad = client.post(
        "/intercom/channels/0/tokens", json={"password": "wrong"}, headers=send_headers
    )
    assert bad.status_code == 401
    assert bad.json()["code"] == "invalid_channel_password"
    # 맞는 비번 → 200.
    ok = client.post(
        "/intercom/channels/0/tokens",
        json={"password": "open123", "name": "요원"},
        headers=send_headers,
    )
    assert ok.status_code == 200
    assert _decode(ok.json()["token"])["name"] == "요원"


def test_enter_unknown_channel_404(client, send_headers):
    assert client.post("/intercom/channels/3/tokens", headers=send_headers).status_code == 404


def test_enter_channel_full_409(client, send_headers, state):
    client.post("/intercom/channels", json={"channel_name": "만원"}, headers=send_headers)
    from livekit.protocol.models import ParticipantInfo

    room = state.intercom_channel_room(0)
    state.livekit.participants[room] = [
        ParticipantInfo(identity=f"intercom-p{i}") for i in range(8)
    ]
    r = client.post("/intercom/channels/0/tokens", headers=send_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "intercom_full"


# ---- 세대 회전·재시작 정리 ----


def test_send_password_rotation_clears_channels(client, send_headers, state):
    client.post("/intercom/channels", json={"channel_name": "임시"}, headers=send_headers)
    assert len(state.db.list_intercom_channels()) == 1
    _run(state.change_send_password("rotated-pw-9"))
    # 세대 회전 후 채널 메타데이터가 비워진다.
    assert state.db.list_intercom_channels() == []


def test_bootstrap_clears_channels(client, send_headers, state):
    client.post("/intercom/channels", json={"channel_name": "재시작전"}, headers=send_headers)
    assert len(state.db.list_intercom_channels()) == 1
    _run(state.bootstrap())
    assert state.db.list_intercom_channels() == []


# ---- 22차 보안 수정 회귀 ----


def test_channel_password_stored_salted_scrypt(client, send_headers, state):
    """채널 비번은 무염 SHA-256 이 아니라 채널별 salt+scrypt 로 저장된다(22차 #2)."""
    client.post(
        "/intercom/channels",
        json={"channel_name": "잠금", "password": "open123"},
        headers=send_headers,
    )
    stored = state.db.get_intercom_channel(0).password_hash
    assert stored.startswith("scrypt$")
    # 같은 비번으로 두 번째 채널을 만들면 salt 가 달라 저장값도 달라야 한다(무염 SHA 면 동일).
    client.post(
        "/intercom/channels",
        json={"channel_name": "잠금2", "password": "open123"},
        headers=send_headers,
    )
    stored2 = state.db.get_intercom_channel(1).password_hash
    assert stored != stored2


def test_channel_password_bruteforce_locks(client, send_headers, state):
    """채널 비번 연속 실패는 송신 비번 성공과 무관하게 (IP, channel) 잠금된다(22차 #1)."""
    client.post(
        "/intercom/channels",
        json={"channel_name": "잠금", "password": "open123"},
        headers=send_headers,
    )
    # PUBLISH_FAIL_LIMIT(5)회 틀리면 이후 요청은 423 로 잠긴다.
    for _ in range(5):
        r = client.post(
            "/intercom/channels/0/tokens", json={"password": "bad"}, headers=send_headers
        )
        assert r.status_code == 401
    locked = client.post(
        "/intercom/channels/0/tokens", json={"password": "open123"}, headers=send_headers
    )
    assert locked.status_code == 423
    assert locked.json()["code"] == "locked"


def test_rotation_atomic_clears_channels_on_failure(client, send_headers, state):
    """송신 비번 회전 트랜잭션이 채널 삭제를 포함한다 — 룸 회전 후 커밋과 원자적(22차 #3)."""
    client.post("/intercom/channels", json={"channel_name": "임시"}, headers=send_headers)
    assert len(state.db.list_intercom_channels()) == 1
    # rotate_send_password 단일 트랜잭션이 채널까지 지우는지 직접 확인.
    new_hash = "0" * 64
    state.db.rotate_send_password("brand-new-pw", state.generation + 1, new_hash)
    assert state.db.list_intercom_channels() == []
