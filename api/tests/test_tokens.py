# 토큰 발급 테스트 — grant 검증(subscribe/publish), identity 규약, 응답 필드
from __future__ import annotations

import jwt

from app.tokens import issue_publish_token, issue_subscribe_token
from tests.conftest import API_KEY, API_SECRET


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def test_subscribe_token_grant():
    token, identity = issue_subscribe_token(API_KEY, API_SECRET, 1, 1)
    assert identity.startswith("listener-")
    claims = _decode(token)
    video = claims["video"]
    assert video["room"] == "field-g1"
    assert video.get("canSubscribe") is True
    # publish 불가.
    assert not video.get("canPublish", False)


def test_publish_token_grant_duplex_microphone():
    identity = "speaker-ch-01-e1-g1-nAbc123"
    token = issue_publish_token(API_KEY, API_SECRET, 1, identity)
    claims = _decode(token)
    video = claims["video"]
    assert video["room"] == "field-g1"
    assert video.get("canPublish") is True
    assert video.get("canSubscribe") is True  # duplex
    # canPublishSources 에 microphone 만.
    assert video.get("canPublishSources") == ["microphone"]
    assert claims["sub"] == identity


def test_subscribe_endpoint_returns_listener_id(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    r = client.post("/channels/1/subscribe-tokens")
    assert r.status_code == 200
    body = r.json()
    assert body["track_name"] == "ch-01"
    assert body["room"] == "field-g1"
    assert body["url"] == "ws://192.168.0.10:7880"
    assert body["ttl_seconds"] == 600
    assert len(body["listener_id"]) == 36


def test_publish_endpoint_identity_and_epoch(client, send_headers):
    client.post("/channels", json={"language": "ko", "label": "한국어"}, headers=send_headers)
    r = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["identity"].startswith("speaker-ch-01-e1-g1-n")
    assert body["epoch"] == 1
    assert body["generation"] == 1
    assert body["ttl_seconds"] == 3600
