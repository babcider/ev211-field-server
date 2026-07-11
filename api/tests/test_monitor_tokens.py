# 관리자 모니터 토큰 테스트 — grant(hidden·구독/발행 불가)·인증·초기 비밀번호 게이트
from __future__ import annotations

import jwt

from app.tokens import issue_monitor_token
from tests.conftest import API_KEY, API_SECRET


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def test_monitor_token_grant_hidden_subscribe_only():
    token, identity = issue_monitor_token(API_KEY, API_SECRET, 1)
    assert identity.startswith("monitor-")
    claims = _decode(token)
    video = claims["video"]
    assert video["room"] == "field-g1"
    assert video.get("hidden") is True
    # 구독 허용(레벨 신호는 subscriber 데이터채널로만 옴) + 발행 불가.
    assert video.get("canSubscribe") is True
    assert not video.get("canPublish", False)
    assert claims["sub"] == identity


def test_monitor_endpoint_requires_admin_password(client, send_headers):
    # 무인증 401.
    r = client.post("/admin/monitor-tokens")
    assert r.status_code == 401
    # 송신자 비밀번호로는 발급 불가(관리자 전용).
    r = client.post("/admin/monitor-tokens", headers=send_headers)
    assert r.status_code == 401


def test_monitor_endpoint_https_required(http_client, admin_headers):
    r = http_client.post(
        "/admin/monitor-tokens",
        headers={**admin_headers, "X-Forwarded-For": "192.168.0.55"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "https_required"


def test_monitor_endpoint_issues_token(client, admin_headers):
    r = client.post("/admin/monitor-tokens", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == "field-g1"
    assert body["url"] == "ws://192.168.0.10:7880"
    assert body["identity"].startswith("monitor-")
    assert body["ttl_seconds"] == 600
    video = _decode(body["token"])["video"]
    assert video.get("hidden") is True
    assert video.get("canSubscribe") is True
    assert not video.get("canPublish", False)


def test_monitor_endpoint_blocks_initial_admin_password(client, state):
    # 초기 비밀번호(0000) 상태에서는 룸 접속 자격을 주지 않는다(13차 #3 규약).
    state.admin_password = "0000"
    r = client.post("/admin/monitor-tokens", headers={"Authorization": "Bearer 0000"})
    assert r.status_code == 403
    assert r.json()["code"] == "must_change_password"


def test_monitor_endpoint_reflects_generation(client, admin_headers, state):
    # 세대 회전 후에는 새 세대 룸으로 발급된다.
    import asyncio

    asyncio.get_event_loop().run_until_complete(state.change_send_password("new-send-pw-1"))
    r = client.post("/admin/monitor-tokens", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == f"field-g{state.generation}"
    assert _decode(body["token"])["video"]["room"] == body["room"]
