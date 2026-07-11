# lease 원자성 테스트 — 중복 409, stale left 무시, nonce 상이, 명시 종료 해제
from __future__ import annotations

from app.config import PUBLISH_TTL_SECONDS
from app.db import Database, new_nonce


def _open(client, headers, cid=1, lang="ko", label="한국어"):
    return client.post("/channels", json={"language": lang, "label": label, "channel_id": cid}, headers=headers)


def test_duplicate_publish_returns_409(client, send_headers):
    _open(client, send_headers)
    r1 = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r1.status_code == 200
    r2 = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r2.status_code == 409
    assert r2.json()["code"] == "channel_busy"


def test_lease_released_after_explicit_close(client, send_headers):
    _open(client, send_headers)
    client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    r = client.delete("/channels/1", headers=send_headers)
    assert r.status_code == 204
    # 재개설 후 다시 발급 가능.
    _open(client, send_headers)
    r2 = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r2.status_code == 200


def test_acquire_lease_atomicity(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    ok1, id1 = db.acquire_lease(1, 1, 1, new_nonce(), PUBLISH_TTL_SECONDS)
    ok2, id2 = db.acquire_lease(1, 1, 1, new_nonce(), PUBLISH_TTL_SECONDS)
    assert ok1 is True and id1
    assert ok2 is False and id2 == ""
    db.close()


def test_stale_left_ignored_on_identity_mismatch(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    _ok, ident = db.acquire_lease(1, 1, 1, "AAA111", PUBLISH_TTL_SECONDS)
    # 다른 identity(구 nonce) 로 온 stale left 는 해제 실패.
    stale = "speaker-ch-01-e1-g1-nOLD999"
    assert db.release_lease_if_identity(1, stale) is False
    assert db.get_lease(1) is not None  # 여전히 살아 있음
    # 정확 일치 identity 는 해제 성공.
    assert db.release_lease_if_identity(1, ident) is True
    assert db.get_lease(1) is None
    db.close()


def test_reacquire_after_ttl_uses_new_nonce(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    # TTL 0 으로 즉시 만료된 lease.
    ok1, id1 = db.acquire_lease(1, 1, 1, "AAA111", 0)
    assert ok1 is True
    # 만료됐으므로 재획득 가능, 새 nonce.
    ok2, id2 = db.acquire_lease(1, 1, 1, "BBB222", PUBLISH_TTL_SECONDS)
    assert ok2 is True
    assert id1 != id2
    # 이전 연결의 지연된 left(id1)는 현재 lease(id2)와 불일치 → 무시.
    assert db.release_lease_if_identity(1, id1) is False
    assert db.get_lease(1).identity == id2
    db.close()
