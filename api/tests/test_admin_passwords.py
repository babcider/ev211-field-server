# 관리자 비밀번호 변경·송신자 비밀번호 변경(세대 회전)·채널 삭제 admin 허용을 검증하는 테스트
from __future__ import annotations

import asyncio

from app.db import Database
from app.state import AppState
from tests.conftest import ADMIN_PW, SEND_PW, MockLiveKit, make_settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _auth(pw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {pw}"}


# ---- 관리자 비밀번호 변경 ----


def test_change_admin_password_keeps_generation(client, state):
    gen_before = state.generation
    r = client.put(
        "/admin/passwords/admin", json={"new_password": "4321"}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 204
    # 세대는 유지(방송 중단 없음).
    assert state.generation == gen_before
    # 새 비밀번호로 admin/status 성공, 옛 비밀번호는 401.
    assert client.get("/admin/status", headers=_auth("4321")).status_code == 200
    assert client.get("/admin/status", headers=_auth(ADMIN_PW)).status_code == 401


def test_change_admin_password_survives_restart_without_generation_bump(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())
    gen = st.generation
    _run(st.change_admin_password("4321"))
    db.close()

    # 재시작 시뮬레이션: 같은 DB 로 새 AppState 구성 → DB 오버라이드가 적용되고 세대 유지.
    db2 = Database(settings.db_path)
    st2 = AppState(settings, db2, MockLiveKit())
    _run(st2.bootstrap())
    assert st2.admin_password == "4321", "재시작 후에도 변경된 관리자 비밀번호가 유지되어야 한다"
    assert st2.generation == gen, "관리자 비밀번호 변경은 재시작 후에도 세대를 올리면 안 된다"
    db2.close()


def test_change_admin_password_rejects_same_as_send(client):
    r = client.put(
        "/admin/passwords/admin", json={"new_password": SEND_PW}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 400


def test_change_admin_password_requires_admin_auth(client):
    r = client.put(
        "/admin/passwords/admin", json={"new_password": "4321"}, headers=_auth(SEND_PW)
    )
    assert r.status_code == 401


def test_change_password_validates_length(client):
    r = client.put(
        "/admin/passwords/admin", json={"new_password": "123"}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 422


# ---- 송신자 비밀번호 변경(세대 회전) ----


def test_change_send_password_rotates_generation(client, state):
    gen_before = state.generation
    # 회전 전 lease 를 하나 만들어 폐기되는지 확인한다.
    state.db.create_channel(1, "ko", "한국어")
    r = client.post("/publish-tokens", json={"channel_id": 1}, headers=_auth(SEND_PW))
    assert r.status_code == 200
    assert state.db.get_lease(1) is not None

    r = client.put(
        "/admin/passwords/send", json={"new_password": "new-send-pw-1"}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 204
    assert state.generation == gen_before + 1, "송신자 비밀번호 변경은 세대를 회전해야 한다"
    assert state.db.get_lease(1) is None, "세대 회전 시 기존 lease 는 전부 해제되어야 한다"

    # 새 비밀번호로 발급 성공, 옛 비밀번호는 401.
    ok = client.post("/publish-tokens", json={"channel_id": 1}, headers=_auth("new-send-pw-1"))
    assert ok.status_code == 200
    old = client.post("/publish-tokens", json={"channel_id": 1}, headers=_auth(SEND_PW))
    assert old.status_code == 401


def test_change_send_password_rejects_same_as_admin(client):
    r = client.put(
        "/admin/passwords/send", json={"new_password": ADMIN_PW}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 400


# ---- 채널 삭제: 관리자 비밀번호 허용 ----


def test_close_channel_accepts_admin_password(client, state):
    state.db.create_channel(2, "en", "English")
    r = client.delete("/channels/2", headers=_auth(ADMIN_PW))
    assert r.status_code == 204, "관리자 비밀번호로도 채널을 삭제할 수 있어야 한다"
    ch = state.db.get_channel(2)
    assert ch is not None and ch.state != "open"


def test_close_channel_still_accepts_send_password(client, state):
    state.db.create_channel(3, "en", "English")
    r = client.delete("/channels/3", headers=_auth(SEND_PW))
    assert r.status_code == 204


def test_close_channel_rejects_wrong_password(client, state):
    state.db.create_channel(4, "en", "English")
    r = client.delete("/channels/4", headers=_auth("wrong-password"))
    assert r.status_code == 401


# ==== codex 12차 결함 회귀 ====


class _RotateFailLK(MockLiveKit):
    """delete_room 이 항상 실패하는 mock — 룸 회전 실패 원자성 검증용."""

    async def delete_room(self, name: str) -> None:
        raise RuntimeError("livekit delete_room connection refused")


def test_send_password_rotation_failure_is_atomic(tmp_path):
    """12차 #1: 룸 회전(LiveKit) 실패 시 비밀번호·세대는 변경 전 그대로여야 한다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())
    gen = st.generation

    from app.main import create_app
    from fastapi.testclient import TestClient

    st.livekit = _RotateFailLK()
    st.livekit.rooms = [st.room]
    app = create_app(state=st)
    with TestClient(app, base_url="https://testserver") as client:
        r = client.put(
            "/admin/passwords/send",
            json={"new_password": "rotate-fail-pw"},
            headers=_auth(ADMIN_PW),
        )
        assert r.status_code == 502, "룸 회전 실패는 502 를 반환해야 한다"
        assert st.generation == gen, "실패 시 세대가 변경되면 안 된다"
        assert st.send_password == SEND_PW, "실패 시 비밀번호가 변경되면 안 된다"
        assert db.get_setting("send_password") is None, "실패 시 DB 에 영속되면 안 된다"
    db.close()


def test_publish_with_old_password_serialized_behind_rotation(tmp_path):
    """14차: 회전 진행 중 발급 요청은 회전 락에서 대기하고, 회전 뒤 구 비밀번호는 401.

    (구 12차 몽키패치 재현 테스트는 회전 락 도입으로 임계구역 안 회전이 불가능해져
    — 자기 락 대기 — 직렬화 검증으로 대체했다.)
    """
    import httpx

    settings = make_settings(tmp_path)
    db = Database(settings.db_path)

    release = asyncio.Event()

    class SlowRotateLK(MockLiveKit):
        async def delete_room(self, name: str) -> None:
            await release.wait()
            await super().delete_room(name)

    lk = SlowRotateLK()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    st.db.create_channel(1, "ko", "한국어")

    from app.main import create_app

    app = create_app(state=st)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            rotate_task = asyncio.create_task(st.change_send_password("rotated-slow-pw"))
            await asyncio.sleep(0.01)  # 회전이 락을 잡고 delete_room 에서 대기하게 한다.
            publish_task = asyncio.create_task(
                client.post(
                    "/publish-tokens", json={"channel_id": 1}, headers=_auth(SEND_PW)
                )
            )
            await asyncio.sleep(0.05)
            assert not publish_task.done(), "발급 요청은 회전 완료까지 회전 락에서 대기해야 한다"
            release.set()
            await rotate_task
            resp = await publish_task
            assert resp.status_code == 401, "회전 후 구 비밀번호 발급 요청은 401 이어야 한다"
            assert st.db.get_lease(1) is None, "발급 거부 시 lease 를 잡으면 안 된다"

    _run(scenario())
    db.close()


def new_nonce_for_test() -> str:
    from app.db import new_nonce

    return new_nonce()


def test_new_password_rejects_initial_default_and_non_ascii(client):
    """12차 #3·#4: 초기값 0000 재사용 금지, 비ASCII 비밀번호 금지."""
    r = client.put(
        "/admin/passwords/admin", json={"new_password": "0000"}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 422, "초기 기본값 0000 은 새 비밀번호로 쓸 수 없어야 한다"
    r = client.put(
        "/admin/passwords/admin", json={"new_password": "한글비밀번호"}, headers=_auth(ADMIN_PW)
    )
    assert r.status_code == 422, "비ASCII 비밀번호는 거부해야 한다(상수시간 비교 호환)"


def test_auth_bearer_handles_non_ascii_token():
    """12차 #4: 비ASCII Bearer 토큰이 와도 TypeError 없이 False 를 반환해야 한다."""
    from app.main import _auth_bearer

    assert _auth_bearer("Bearer 한글토큰", "ascii-pw") is False
    assert _auth_bearer("Bearer ascii-pw", "ascii-pw") is True


# ==== codex 13차 결함 회귀 ====


def test_concurrent_send_rotation_serialized(tmp_path):
    """13차 #2: 동시 회전은 직렬화되어 서로 다른 세대를 쓰고 둘 다 반영된다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())
    gen0 = st.generation

    async def both():
        await asyncio.gather(
            st.change_send_password("concurrent-pw-1"),
            st.change_send_password("concurrent-pw-2"),
        )

    _run(both())
    assert st.generation == gen0 + 2, "동시 회전은 직렬화되어 세대가 2 증가해야 한다"
    assert st.send_password == "concurrent-pw-2"
    # DB 영속값도 최종 상태와 일치.
    assert db.get_setting("send_password") == "concurrent-pw-2"
    db.close()


def test_concurrent_admin_and_send_same_value_rejected(tmp_path):
    """13차 #2: 관리자·송신자 비밀번호가 동시 변경으로 같은 값이 되는 경쟁을 차단한다."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())

    async def both():
        return await asyncio.gather(
            st.change_send_password("same-value-pw"),
            st.change_admin_password("same-value-pw"),
            return_exceptions=True,
        )

    results = _run(both())
    errors = [r for r in results if isinstance(r, ValueError)]
    assert len(errors) == 1, "둘 중 하나는 동일값으로 거부되어야 한다"
    assert st.send_password != st.admin_password, "최종 상태에서 두 비밀번호는 달라야 한다"
    db.close()


def _initial_admin_state(tmp_path):
    """초기 관리자 비밀번호(0000) 상태의 AppState 를 구성한다."""
    import dataclasses

    settings = dataclasses.replace(make_settings(tmp_path), admin_password="0000")
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())
    return st, db


def test_initial_admin_password_gates_privileged_ops(tmp_path):
    """13차 #3: 초기 0000 상태에서는 변경·상태조회 외 관리자 작업을 서버가 403 으로 거부한다."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    st, db = _initial_admin_state(tmp_path)
    st.db.create_channel(1, "ko", "한국어")
    app = create_app(state=st)
    with TestClient(app, base_url="https://testserver") as client:
        # 로그인 확인용 상태 조회는 허용(읽기 전용).
        assert client.get("/admin/status", headers=_auth("0000")).status_code == 200
        # 특권 작업은 403 must_change_password.
        r = client.post("/admin/channels/1/takeover", headers=_auth("0000"))
        assert r.status_code == 403 and r.json()["code"] == "must_change_password"
        r = client.put(
            "/admin/passwords/send", json={"new_password": "new-send-9"}, headers=_auth("0000")
        )
        assert r.status_code == 403
        r = client.delete("/channels/1", headers=_auth("0000"))
        assert r.status_code == 403
        # 관리자 비밀번호 변경만은 허용(강제 변경 경로) → 이후 특권 작업 가능.
        r = client.put(
            "/admin/passwords/admin", json={"new_password": "fresh-admin-9"}, headers=_auth("0000")
        )
        assert r.status_code == 204
        r = client.delete("/channels/1", headers=_auth("fresh-admin-9"))
        assert r.status_code == 204
    db.close()


def test_concurrent_initial_admin_changes_only_first_wins(tmp_path):
    """14차 #2: 초기 0000 으로 동시에 온 두 변경 요청 중 첫 요청만 성공해야 한다.

    락 획득 후 인증을 재검증하므로, 첫 요청이 비밀번호를 바꾸면 대기 중이던 두 번째
    0000 요청은 401 로 거부된다(뒤늦은 덮어쓰기 차단).
    """
    import dataclasses

    import httpx

    settings = dataclasses.replace(make_settings(tmp_path), admin_password="0000")
    db = Database(settings.db_path)
    st = AppState(settings, db, MockLiveKit())
    _run(st.bootstrap())

    from app.main import create_app

    app = create_app(state=st)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            r1, r2 = await asyncio.gather(
                client.put(
                    "/admin/passwords/admin",
                    json={"new_password": "first-admin-pw"},
                    headers=_auth("0000"),
                ),
                client.put(
                    "/admin/passwords/admin",
                    json={"new_password": "second-admin-pw"},
                    headers=_auth("0000"),
                ),
            )
            codes = sorted([r1.status_code, r2.status_code])
            assert codes == [204, 401], f"첫 변경만 성공해야 한다: {codes}"
            assert st.admin_password in {"first-admin-pw", "second-admin-pw"}

    _run(scenario())
    db.close()
