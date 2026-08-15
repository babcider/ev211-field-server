# 라이브별 송신 비번 콜백 인증 테스트 — 복합 Bearer 분리·캐시·fail-closed·모드 분기 (계약 §7)
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.live_auth import LiveSendVerifier
from app.main import create_app
from app.state import AppState
from tests.conftest import SEND_PW, MockLiveKit, make_settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---- split_bearer 단위 ----

def test_split_bearer_parses_and_normalizes():
    assert LiveSendVerifier.split_bearer("k3m7pq:483920") == ("K3M7PQ", "483920")
    assert LiveSendVerifier.split_bearer("K3M7PQ:pw:with:colons") == ("K3M7PQ", "pw:with:colons")


def test_split_bearer_rejects_bad_forms():
    for bad in (None, "", "passwordonly", ":pw", "code:", "  :  "):
        assert LiveSendVerifier.split_bearer(bad) is None


# ---- verify 캐시·fail-closed ----

class CountingVerifier(LiveSendVerifier):
    """콜백 호출 횟수를 세는 테스트 대역 — _call_verify 만 대체한다."""

    def __init__(self, ok: bool = True) -> None:
        super().__init__("https://ev211.test", "secret")
        self.calls = 0
        self.ok = ok

    async def _call_verify(self, join_code: str, send_password: str) -> bool:
        self.calls += 1
        return self.ok


def test_verify_caches_positive_result_only():
    v = CountingVerifier(ok=True)
    assert _run(v.verify("K3M7PQ:483920")) is True
    assert _run(v.verify("K3M7PQ:483920")) is True
    assert v.calls == 1  # 두 번째는 캐시 히트

    denied = CountingVerifier(ok=False)
    assert _run(denied.verify("K3M7PQ:000000")) is False
    assert _run(denied.verify("K3M7PQ:000000")) is False
    assert denied.calls == 2  # 부정 결과는 캐시하지 않는다


def test_verify_rejects_non_compound_bearer_without_call():
    v = CountingVerifier(ok=True)
    assert _run(v.verify(SEND_PW)) is False  # 콜론 없는 전역 비번 형식은 콜백 대상 아님
    assert v.calls == 0


# ---- 모드 분기(엔드포인트 통합) ----

def _make_state(tmp_path, mode: str) -> AppState:
    settings = dataclasses.replace(
        make_settings(tmp_path),
        send_auth_mode=mode,
        ev211_api_base="https://ev211.test",
        field_callback_secret="secret",
    )
    st = AppState(settings, Database(settings.db_path), MockLiveKit())
    _run(st.bootstrap())
    return st


@pytest.fixture
def callback_client(tmp_path):
    st = _make_state(tmp_path, "callback")
    st.live_verifier = CountingVerifier(ok=True)
    with TestClient(create_app(state=st), base_url="https://testserver") as c:
        yield c, st
    st.db.close()


def test_callback_mode_accepts_compound_bearer(callback_client):
    c, st = callback_client
    r = c.post(
        "/channels",
        json={"channel_id": 1, "language": "en", "label": "통역"},
        headers={"Authorization": "Bearer K3M7PQ:483920"},
    )
    assert r.status_code == 201, r.text
    assert st.live_verifier.calls == 1


def test_callback_mode_rejects_global_password(callback_client):
    c, _ = callback_client
    r = c.post(
        "/channels",
        json={"channel_id": 1, "language": "en", "label": "통역"},
        headers={"Authorization": f"Bearer {SEND_PW}"},
    )
    assert r.status_code == 401


def test_both_mode_accepts_either(tmp_path):
    st = _make_state(tmp_path, "both")
    st.live_verifier = CountingVerifier(ok=True)
    with TestClient(create_app(state=st), base_url="https://testserver") as c:
        ok_pw = c.post(
            "/channels",
            json={"channel_id": 1, "language": "en", "label": "통역"},
            headers={"Authorization": f"Bearer {SEND_PW}"},
        )
        assert ok_pw.status_code == 201, ok_pw.text
        ok_cb = c.post(
            "/channels",
            json={"channel_id": 2, "language": "ja", "label": "통역2"},
            headers={"Authorization": "Bearer K3M7PQ:483920"},
        )
        assert ok_cb.status_code == 201, ok_cb.text
    st.db.close()
