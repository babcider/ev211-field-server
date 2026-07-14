# AI 통역 채널 엔드포인트 테스트 — CRUD·인증·사람송신 차단·마이그레이션 멱등성(워커 mock)
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import _close_dependent_ai_channels, create_app
from app.state import AppState
from app.translate_worker import AiChannelManager, AiWorkerParams
from tests.conftest import ADMIN_PW, MockLiveKit, make_settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeWorker:
    """실 rtc/OpenAI 접속 없이 stop 까지 파킹만 하는 워커 대역."""

    session_age_seconds = None
    last_audio_at = None
    seq = 0
    renewal_due = False
    ready = None  # 슈퍼바이저가 채널 공용 준비 이벤트를 주입한다(MED8).
    token_provider = None

    def __init__(self):
        self.stopped = False

    async def run(self, stop):
        # 실제 접속 성공을 대신해 준비 완료를 즉시 알린다(개설 엔드포인트 readiness 게이트).
        if self.ready is not None:
            self.ready.set()
        await stop.wait()

    async def aclose(self):
        self.stopped = True


def _build_client(tmp_path, openai_api_key="sk-test"):
    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key=openai_api_key)
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    # 실 워커 대신 fake 워커 매니저로 교체(백오프 0 으로 즉시 동작).
    st.ai_channels = AiChannelManager(
        lambda params: FakeWorker(), backoff_base=0.0, max_backoff=0.0
    )
    app = create_app(state=st)
    return st, db, app


@pytest.fixture
def ai_client(tmp_path):
    st, db, app = _build_client(tmp_path)
    with TestClient(app, base_url="https://testserver") as c:
        yield c, st
    db.close()


@pytest.fixture
def send_headers():
    from tests.conftest import SEND_PW

    return {"Authorization": f"Bearer {SEND_PW}"}


# ---- 인증 ----
def test_create_requires_send_password(ai_client):
    client, _st = ai_client
    assert client.post("/ai-channels", json={"target_language": "en"}).status_code == 401
    # 관리자 비번은 불가(송신자 전용).
    r = client.post(
        "/ai-channels",
        json={"target_language": "en"},
        headers={"Authorization": f"Bearer {ADMIN_PW}"},
    )
    assert r.status_code == 401


def test_status_requires_send_password(ai_client):
    client, _st = ai_client
    assert client.get("/ai-channels/1/status").status_code == 401


# ---- 개설 ----
def test_create_ai_channel_success(ai_client, send_headers):
    client, st = ai_client
    r = client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["channel_id"] == 1  # Floor(0) 제외 최저 슬롯
    assert body["target_language"] == "en"
    assert body["source"] == "ai"
    assert body["track_name"] == "ch-01"
    assert body["source_channel"] == 0
    assert body["identity"].startswith("speaker-ch-01-")
    # 워커가 스폰돼 있고 채널에 lease 가 잡혀 있다.
    assert st.ai_channels.has(1) is True
    assert st.db.get_lease(1) is not None
    # /channels 목록에 AI 채널로 노출된다.
    listed = client.get("/channels").json()["channels"]
    ai = [c for c in listed if c["channel_id"] == 1][0]
    assert ai["source"] == "ai"
    assert ai["target_language"] == "en"


def test_create_rejects_unsupported_language(ai_client, send_headers):
    client, _st = ai_client
    r = client.post("/ai-channels", json={"target_language": "xx"}, headers=send_headers)
    assert r.status_code == 422


def test_create_requires_openai_key(tmp_path, send_headers):
    st, db, app = _build_client(tmp_path, openai_api_key="")
    try:
        with TestClient(app, base_url="https://testserver") as client:
            r = client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
            assert r.status_code == 503
    finally:
        db.close()


# ---- 사람 송신 차단 ----
def test_human_publish_to_ai_channel_blocked(ai_client, send_headers):
    client, _st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    r = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "channel_busy"


# ---- 상태 ----
def test_ai_channel_status(ai_client, send_headers):
    client, _st = ai_client
    client.post("/ai-channels", json={"target_language": "ko"}, headers=send_headers)
    r = client.get("/ai-channels/1/status", headers=send_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["channel_id"] == 1
    assert body["target_language"] == "ko"
    assert body["worker"]["running"] is True
    # 존재하지 않는 AI 채널은 404.
    assert client.get("/ai-channels/5/status", headers=send_headers).status_code == 404


# ---- 종료 ----
def test_delete_ai_channel(ai_client, send_headers):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert st.ai_channels.has(1) is True

    r = client.delete("/ai-channels/1", headers=send_headers)
    assert r.status_code == 204
    # 워커 정지 + 채널 종료.
    assert st.ai_channels.has(1) is False
    assert st.db.get_channel(1).state == "closed"
    # 재삭제는 404.
    assert client.delete("/ai-channels/1", headers=send_headers).status_code == 404


def test_delete_rejects_non_ai_channel(ai_client, send_headers):
    client, _st = ai_client
    # 사람 채널을 만든 뒤 AI 삭제 엔드포인트로 지우려 하면 404.
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 2},
        headers=send_headers,
    )
    assert client.delete("/ai-channels/2", headers=send_headers).status_code == 404


# ---- 마이그레이션 멱등성 ----
def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "mig.db")
    db1 = Database(path)
    ch = db1.create_channel(1, "en", "AI: en", source="ai", target_language="en")
    assert ch.source == "ai"
    assert ch.target_language == "en"
    db1.close()
    # 같은 파일을 다시 열어 _migrate 가 재실행돼도 오류 없이 데이터가 유지된다.
    db2 = Database(path)
    got = db2.get_channel(1)
    assert got.source == "ai"
    assert got.target_language == "en"
    # 한 번 더 열어도(3회차) ALTER 중복 오류가 없다.
    db2.close()
    db3 = Database(path)
    assert db3.get_channel(1).source == "ai"
    db3.close()


def test_default_channel_source_is_human(tmp_path):
    db = Database(str(tmp_path / "h.db"))
    ch = db.create_channel(1, "ko", "한국어")
    assert ch.source == "human"
    assert ch.target_language is None
    db.close()


# ---- MED10: 마이그레이션 동시 기동 경합 ----
def test_migrate_tolerates_concurrent_duplicate_add(tmp_path, monkeypatch):
    # 존재 확인은 '없음'을 반환하지만 실제 테이블엔 이미 컬럼이 있어 ALTER 가 duplicate 로
    # 실패하는 다중 프로세스 기동 경합을 흉내낸다 — 헬퍼가 duplicate 오류를 삼켜야 한다.
    db = Database(str(tmp_path / "m.db"))
    monkeypatch.setattr(db, "_query", lambda sql, params=(): [])
    db._add_column_if_missing("channels", "source", "TEXT NOT NULL DEFAULT 'human'")  # 예외 없이 통과
    db.close()


# ---- HIGH3: 일반 DELETE 로 AI 워커·슈퍼바이저 정지 ----
def test_general_delete_stops_ai_worker(ai_client, send_headers):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert st.ai_channels.has(1) is True
    r = client.delete("/channels/1", headers=send_headers)
    assert r.status_code == 204
    # 슈퍼바이저 정지 + 채널 종료(비용 누수·strike 루프 방지).
    assert st.ai_channels.has(1) is False
    assert st.db.get_channel(1).state == "closed"


# ---- HIGH4: 관리자 takeover 는 AI 채널을 거부 ----
def test_takeover_rejects_ai_channel(ai_client, send_headers):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    r = client.post(
        "/admin/channels/1/takeover",
        headers={"Authorization": f"Bearer {ADMIN_PW}"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "channel_busy"
    # 인수 거부이므로 워커·lease 는 그대로 살아 있다.
    assert st.ai_channels.has(1) is True
    assert st.db.get_lease(1) is not None


# ---- HIGH2: 재시작(bootstrap)·송신 비번 회전 시 고아 AI 채널 정리 ----
def test_bootstrap_closes_orphan_ai_channels(tmp_path):
    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key="sk-test")
    db = Database(settings.db_path)
    # 이전 프로세스가 남긴 open AI 채널(인메모리 워커 없음)을 흉내낸다.
    db.create_channel(3, "en", "AI: en", source="ai", target_language="en")
    db.acquire_lease(3, 1, 1, "abc123", 3600, 120)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())
    ch = db.get_channel(3)
    assert ch.state == "closed"  # 고아 채널 정리
    assert db.get_lease(3) is None  # 슬롯 반납
    db.close()


def test_send_password_rotation_closes_ai_channels(ai_client, send_headers):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert st.db.get_channel(1).state == "open"
    # 송신 비번 회전(세대 회전)을 엔드포인트로 트리거한다 — 슈퍼바이저 task 와 같은
    # 이벤트루프(TestClient)에서 실행돼야 stop 이 완료를 관찰한다(교차 루프 회피).
    r = client.put(
        "/admin/passwords/send",
        json={"new_password": "rotated-send-9999"},
        headers={"Authorization": f"Bearer {ADMIN_PW}"},
    )
    assert r.status_code == 204
    # 인메모리 워커 정지 + DB 행도 닫혀 슬롯이 반납된다.
    assert st.ai_channels.has(1) is False
    assert st.db.get_channel(1).state == "closed"


# ---- MED9: 원음 채널 검증 ----
def test_create_ai_channel_rejects_self_source(ai_client, send_headers):
    client, _st = ai_client
    # 출력 슬롯(최저=1)과 원음 채널이 같으면 자기 트랙 구독 → 409.
    r = client.post(
        "/ai-channels",
        json={"target_language": "en", "source_channel": 1},
        headers=send_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_source"


def test_create_ai_channel_rejects_closed_source(ai_client, send_headers):
    client, _st = ai_client
    # 존재하지 않는(닫힌) 비-Floor 원음 채널은 409.
    r = client.post(
        "/ai-channels",
        json={"target_language": "en", "source_channel": 5},
        headers=send_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_source"


def test_create_ai_channel_accepts_open_nonfloor_source(ai_client, send_headers):
    client, _st = ai_client
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 2},
        headers=send_headers,
    )
    r = client.post(
        "/ai-channels",
        json={"target_language": "en", "source_channel": 2},
        headers=send_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["source_channel"] == 2


# ---- MED11: 슬롯 경합으로 인한 ChannelExists → 409 ----
def test_create_ai_channel_slot_conflict_returns_409(ai_client, send_headers, monkeypatch):
    from app.db import ChannelExists

    client, st = ai_client

    def boom(*a, **k):
        raise ChannelExists(1)

    monkeypatch.setattr(st.db, "create_channel", boom)
    r = client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert r.status_code == 409  # 500 이 아니라 409
    assert r.json()["code"] == "max_channels_reached"


# ---- MED8: 워커가 제한시간 내 준비되지 않으면 503 + 롤백 ----
def test_create_ai_channel_readiness_timeout_rolls_back(tmp_path, send_headers, monkeypatch):
    import app.main as main_mod

    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key="sk-test")
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())

    class NeverReadyWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False
        ready = None
        token_provider = None

        def __init__(self):
            self.stopped = False

        async def run(self, stop):
            # 준비 신호 없이 즉시 크래시 → 슈퍼바이저가 계속 재시작하지만 ready 는 안 뜬다.
            raise RuntimeError("connect fail")

        async def aclose(self):
            self.stopped = True

    st.ai_channels = AiChannelManager(
        lambda params: NeverReadyWorker(), backoff_base=0.05, max_backoff=0.05
    )
    monkeypatch.setattr(main_mod, "AI_READY_TIMEOUT_SECONDS", 0.2)
    app = create_app(state=st)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            r = client.post(
                "/ai-channels", json={"target_language": "en"}, headers=send_headers
            )
            assert r.status_code == 503
            # 롤백: 워커 정지 + 채널 종료.
            assert st.ai_channels.has(1) is False
            assert st.db.get_channel(1).state == "closed"
    finally:
        db.close()


# ---- 회귀1: 타임아웃 롤백이 재사용된 슬롯의 엉뚱한 채널을 닫지 않는다 ----
def test_readiness_rollback_skips_when_slot_reused(tmp_path, send_headers, monkeypatch):
    import app.main as main_mod

    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key="sk-test")
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())

    class ParkNoReadyWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False
        ready = None
        token_provider = None

        def __init__(self):
            self.stopped = False

        async def run(self, stop):
            await stop.wait()  # 준비 신호 없이 파킹 → readiness 타임아웃 유발

        async def aclose(self):
            self.stopped = True

    st.ai_channels = AiChannelManager(
        lambda params: ParkNoReadyWorker(), backoff_base=0.0, max_backoff=0.0
    )
    monkeypatch.setattr(main_mod, "AI_READY_TIMEOUT_SECONDS", 0.2)
    # 대기 중 이 슬롯이 삭제→재개설로 재사용된 상황: get 이 다른 객체를 반환하게 한다.
    monkeypatch.setattr(st.ai_channels, "get", lambda cid: object())
    app = create_app(state=st)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            r = client.post(
                "/ai-channels", json={"target_language": "en"}, headers=send_headers
            )
            assert r.status_code == 503
            # 슬롯 재사용으로 판정 → 엉뚱한 채널을 닫지 않았다(행 유지).
            assert st.db.get_channel(1).state == "open"
    finally:
        db.close()


# ---- 회귀2: AI 채널의 일반 DELETE 는 LiveKit 제거 실패에도 항상 close ----
def test_general_delete_ai_channel_closes_despite_livekit_failure(
    ai_client, send_headers, monkeypatch
):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)

    async def boom(room, identity):
        raise RuntimeError("livekit connection refused")

    monkeypatch.setattr(st.livekit, "remove_participant", boom)
    r = client.delete("/channels/1", headers=send_headers)
    assert r.status_code == 204  # 502 가 아니라 204(전용 AI DELETE 와 일관)
    assert st.ai_channels.has(1) is False
    assert st.db.get_channel(1).state == "closed"  # open 고아 채널을 남기지 않는다


def test_general_delete_human_channel_502_on_livekit_failure(
    ai_client, send_headers, monkeypatch
):
    # 대조군: 사람 채널은 종전대로 502(고아 방지 fail-closed)를 유지한다.
    client, st = ai_client
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 2},
        headers=send_headers,
    )
    client.post("/publish-tokens", json={"channel_id": 2}, headers=send_headers)

    async def boom(room, identity):
        raise RuntimeError("livekit connection refused")

    monkeypatch.setattr(st.livekit, "remove_participant", boom)
    r = client.delete("/channels/2", headers=send_headers)
    assert r.status_code == 502
    assert st.db.get_channel(2).state == "open"  # 닫히지 않음


# ---- 미흡(codex 3차): 운영 DELETE 경로가 bounded stop timeout 을 전달하는가 ----
def _record_stop_timeouts(st, monkeypatch):
    """st.ai_channels.stop 을 감싸 전달된 timeout 을 기록한다(무한 대기 회귀 방지)."""
    real_stop = st.ai_channels.stop
    seen: list[float | None] = []

    async def rec_stop(channel_id, timeout=None):
        seen.append(timeout)
        return await real_stop(channel_id, timeout=timeout)

    monkeypatch.setattr(st.ai_channels, "stop", rec_stop)
    return seen


def test_general_delete_passes_bounded_stop_timeout(ai_client, send_headers, monkeypatch):
    # 일반 DELETE /channels 의 AI 분기가 timeout 없이 stop 을 호출하면 aclose 고착 시
    # 요청이 무한 대기한다(codex 3차 미흡). bounded timeout 전달을 잠근다.
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    seen = _record_stop_timeouts(st, monkeypatch)
    r = client.delete("/channels/1", headers=send_headers)
    assert r.status_code == 204
    assert seen and seen[-1] is not None  # bounded stop 전달됨


def test_dedicated_ai_delete_passes_bounded_stop_timeout(
    ai_client, send_headers, monkeypatch
):
    client, st = ai_client
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    seen = _record_stop_timeouts(st, monkeypatch)
    r = client.delete("/ai-channels/1", headers=send_headers)
    assert r.status_code == 204
    assert seen and seen[-1] is not None


# ---- 결함2: readiness 타임아웃 롤백이 잔존 participant 를 제거한다 ----
def test_readiness_rollback_removes_participant(tmp_path, send_headers, monkeypatch):
    import app.main as main_mod

    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key="sk-test")
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())

    class ParkNoReadyWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False
        ready = None
        token_provider = None

        def __init__(self):
            self.stopped = False

        async def run(self, stop):
            await stop.wait()  # 준비 신호 없이 파킹 → readiness 타임아웃

        async def aclose(self):
            self.stopped = True

    st.ai_channels = AiChannelManager(
        lambda params: ParkNoReadyWorker(), backoff_base=0.0, max_backoff=0.0
    )
    monkeypatch.setattr(main_mod, "AI_READY_TIMEOUT_SECONDS", 0.2)
    removed = []
    orig_remove = lk.remove_participant

    async def spy(room, identity):
        removed.append(identity)
        return await orig_remove(room, identity)

    monkeypatch.setattr(lk, "remove_participant", spy)
    app = create_app(state=st)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            r = client.post(
                "/ai-channels", json={"target_language": "en"}, headers=send_headers
            )
            assert r.status_code == 503
            # 롤백이 lease identity 로 잔존 participant 를 제거했다(DELETE 경로와 일관).
            assert removed and removed[-1].startswith("speaker-ch-01-")
            assert st.db.get_channel(1).state == "closed"
    finally:
        db.close()


# ---- 결함4: source_channel 영속 + 순환 검사 + cascade ----
def test_ai_channel_source_channel_persisted(ai_client, send_headers):
    client, st = ai_client
    # Floor(0) 원음 기본값이 영속된다.
    client.post("/ai-channels", json={"target_language": "en"}, headers=send_headers)
    assert st.db.get_channel(1).source_channel == 0
    # 비-Floor 원음도 영속된다.
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 3},
        headers=send_headers,
    )
    client.post(
        "/ai-channels",
        json={"target_language": "ja", "source_channel": 3},
        headers=send_headers,
    )
    assert st.db.get_channel(2).source_channel == 3


def test_create_ai_channel_rejects_dependency_cycle(ai_client, send_headers):
    client, st = ai_client
    # slot 2 에 원음이 slot 1 인 AI 채널을 직접 seed(ch2 → ch1).
    st.db.create_channel(
        2, "en", "AI: en", source="ai", target_language="en", source_channel=1
    )
    # slot 1 에 원음이 slot 2 인 AI 채널을 만들면 1↔2 순환 → 409.
    r = client.post(
        "/ai-channels",
        json={"target_language": "ja", "source_channel": 2},
        headers=send_headers,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_source"


def test_delete_source_cascades_dependent_ai_channels(ai_client, send_headers):
    client, st = ai_client
    # 사람 원음 ch2 + 그것을 구독하는 AI(cid=1).
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 2},
        headers=send_headers,
    )
    client.post(
        "/ai-channels",
        json={"target_language": "en", "source_channel": 2},
        headers=send_headers,
    )
    assert st.ai_channels.has(1) is True
    assert st.db.get_channel(1).source_channel == 2
    # 원음(사람 ch2) 삭제 → 종속 AI(ch1)를 cascade 정지·close.
    r = client.delete("/channels/2", headers=send_headers)
    assert r.status_code == 204
    assert st.ai_channels.has(1) is False
    assert st.db.get_channel(1).state == "closed"


# ---- 결함4·B: readiness 롤백도 종속 AI 채널을 cascade 종료 ----
def test_readiness_rollback_cascades_dependents(tmp_path, send_headers, monkeypatch):
    import app.main as main_mod

    settings = dataclasses.replace(make_settings(tmp_path), openai_api_key="sk-test")
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    _run(st.bootstrap())

    class ParkWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False
        ready = None
        token_provider = None

        def __init__(self):
            self.stopped = False

        async def run(self, stop):
            await stop.wait()  # 준비 신호 없이 파킹

        async def aclose(self):
            self.stopped = True

    seeded = {"done": False}

    def factory(params):
        # A(cid=1) 워커가 뜨는 순간, 그것(ch-01)을 원음으로 하는 종속 AI B(cid=2)를
        # seed·start 한다 — A 준비 대기 중 B 가 개설된 상황을 흉내.
        if params.channel_id == 1 and not seeded["done"]:
            seeded["done"] = True
            st.db.create_channel(
                2, "en", "AI: en", source="ai", target_language="en", source_channel=1
            )
            st.ai_channels.start(
                AiWorkerParams(
                    channel_id=2,
                    target_language="en",
                    rtc_url="",
                    token="",
                    room="",
                    publish_track="ch-02",
                    subscribe_track="ch-01",
                )
            )
        return ParkWorker()

    st.ai_channels = AiChannelManager(factory, backoff_base=0.0, max_backoff=0.0)
    monkeypatch.setattr(main_mod, "AI_READY_TIMEOUT_SECONDS", 0.2)
    app = create_app(state=st)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            r = client.post(
                "/ai-channels", json={"target_language": "en"}, headers=send_headers
            )
            assert r.status_code == 503  # A(1) readiness 타임아웃
            # A(1) 롤백 + 종속 B(2) cascade 종료.
            assert st.db.get_channel(1).state == "closed"
            assert st.db.get_channel(2).state == "closed"
            assert st.ai_channels.has(2) is False
    finally:
        db.close()


# ---- 결함4·C: cascade 가 list~lock 사이 재사용된 슬롯은 닫지 않는다 ----
def test_cascade_skips_reused_slot(ai_client, send_headers, monkeypatch):
    client, st = ai_client
    # 사람 원음 ch3 + 그것을 구독하는 AI(cid=1).
    client.post(
        "/channels",
        json={"language": "ko", "label": "한국어", "channel_id": 3},
        headers=send_headers,
    )
    client.post(
        "/ai-channels",
        json={"target_language": "en", "source_channel": 3},
        headers=send_headers,
    )
    # cid=1 슬롯을 재사용해 사람 채널로 교체(재사용 흉내).
    st.db.close_channel(1)
    st.db.create_channel(1, "ja", "human-reused")  # source='human', source_channel=None
    # list 는 여전히 stale 하게 cid=1 을 종속으로 돌려주지만(list~lock 레이스), 잠금 안
    # 재조회에서 source!='ai' 이므로 닫지 않아야 한다.
    monkeypatch.setattr(
        st.db, "list_open_ai_channels_by_source", lambda src: [1] if src == 3 else []
    )
    closed = _run(_close_dependent_ai_channels(st, 3))
    assert closed == []  # 재사용된 사람 채널을 닫지 않았다
    assert st.db.get_channel(1).state == "open"  # 그대로 열림
