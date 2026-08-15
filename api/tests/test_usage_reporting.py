# 사용량 세션 파생·보고 테스트 — kind 판정·join_code 귀속·배치 push·재시도 (계약 §4)
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.live_auth import LiveSendVerifier
from app.main import _usage_push_sweep, create_app
from app.state import ACTIVE_JOIN_CODE_KEY, AppState
from app.usage_reporter import MAX_ATTEMPTS, UsageReporter, session_to_event
from tests.conftest import SEND_PW, MockLiveKit, make_settings

JOIN_CODE = "K3M7PQ"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeReporter(UsageReporter):
    """실제 HTTP 대신 전송 배치를 모으는 대역 — _post 만 대체한다."""

    def __init__(self, ok: bool = True) -> None:
        super().__init__("https://ev211.test", "secret")
        self.batches: list[list[dict]] = []
        self.ok = ok

    async def _post(self, events: list[dict]) -> bool:
        self.batches.append(events)
        return self.ok


def _make_state(tmp_path, *, mode: str = "callback") -> AppState:
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
def usage_state(tmp_path):
    st = _make_state(tmp_path)
    st.usage_reporter = FakeReporter()
    st.note_active_join_code(JOIN_CODE)
    yield st
    st.db.close()


def _join(st: AppState, *, direction: str, scope: str, channel_id, subject: str) -> None:
    st.record_signal_event(
        direction=direction,
        event_type="participant_joined",
        scope=scope,
        channel_id=channel_id,
        room=st.room,
        subject=subject,
    )


def _leave(st: AppState, *, direction: str, scope: str, channel_id, subject: str) -> None:
    st.record_signal_event(
        direction=direction,
        event_type="participant_left",
        scope=scope,
        channel_id=channel_id,
        room=st.room,
        subject=subject,
    )


# ---- 이벤트 변환(계약 형식) ----

def test_session_to_event_matches_contract_shape(usage_state):
    st = usage_state
    st.db.create_channel(1, "en", "AI: en", source="ai", target_language="en")
    _join(st, direction="send", scope="relay", channel_id=1, subject="speaker-ch-01-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=1, subject="speaker-ch-01-e1-g1-nAbc")

    row = st.db.pending_usage_sessions()[0]
    event = session_to_event(row)
    assert event["join_code"] == JOIN_CODE
    assert event["kind"] == "ai_translate"
    assert event["language"] == "en"
    assert event["seconds"] >= 0
    assert event["started_at"].endswith("Z") and event["ended_at"].endswith("Z")
    assert event["participant_hash"] == row.subject_hash
    # 원문 identity 는 실어 나르지 않는다(해시만).
    assert "speaker-ch-01" not in str(event)


def test_session_event_omits_language_when_absent(usage_state):
    st = usage_state
    _join(st, direction="both", scope="intercom", channel_id=None, subject="intercom-abc")
    _leave(st, direction="both", scope="intercom", channel_id=None, subject="intercom-abc")

    event = session_to_event(st.db.pending_usage_sessions()[0])
    assert event["kind"] == "intercom"
    assert "language" not in event


# ---- kind 판정 ----

def test_human_channel_publisher_is_send(usage_state):
    st = usage_state
    st.db.create_channel(0, "ko", "원어(한국어)")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    row = st.db.pending_usage_sessions()[0]
    assert (row.kind, row.language) == ("send", "ko")


def test_ai_channel_publisher_is_ai_translate(usage_state):
    st = usage_state
    st.db.create_channel(2, "ru", "AI: ru", source="ai", target_language="ru")
    _join(st, direction="send", scope="relay", channel_id=2, subject="speaker-ch-02-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=2, subject="speaker-ch-02-e1-g1-nAbc")

    row = st.db.pending_usage_sessions()[0]
    assert (row.kind, row.language) == ("ai_translate", "ru")


def test_listener_channel_resolved_from_token_event(usage_state):
    """청취자 참가 웹훅에는 채널이 없다 — 직전 구독 토큰 발급 이벤트에서 찾아야 한다."""
    st = usage_state
    st.db.create_channel(1, "en", "AI: en", source="ai", target_language="en")
    st.record_signal_event(
        direction="receive",
        event_type="token_issued",
        scope="relay",
        channel_id=1,
        room=st.room,
        subject="listener-uuid-1",
    )
    _join(st, direction="receive", scope="relay", channel_id=None, subject="listener-uuid-1")
    _leave(st, direction="receive", scope="relay", channel_id=None, subject="listener-uuid-1")

    row = st.db.pending_usage_sessions()[0]
    assert (row.kind, row.channel_id, row.language) == ("listen", 1, "en")


# ---- join_code 귀속 ----

def test_no_session_without_active_live(tmp_path):
    """내부망(비번 모드)처럼 귀속 라이브가 없으면 집계하지 않는다."""
    st = _make_state(tmp_path, mode="password")
    st.usage_reporter = FakeReporter()
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    assert st.db.pending_usage_sessions() == []
    st.db.close()


def test_active_join_code_persists_and_restores(tmp_path):
    st = _make_state(tmp_path)
    st.note_active_join_code("k3m7pq")
    assert st.active_join_code == JOIN_CODE  # 대문자 정규화
    assert st.db.get_setting(ACTIVE_JOIN_CODE_KEY) == JOIN_CODE
    st.db.close()

    # 같은 DB 로 재기동하면 진행 중이던 라이브가 복원된다.
    settings = dataclasses.replace(
        make_settings(tmp_path), send_auth_mode="callback",
        ev211_api_base="https://ev211.test", field_callback_secret="secret",
    )
    revived = AppState(settings, Database(settings.db_path), MockLiveKit())
    assert revived.active_join_code == JOIN_CODE
    revived.db.close()


def test_open_session_keeps_original_live_after_switch(usage_state):
    """라이브가 교체돼도 이미 열린 세션의 귀속은 바뀌지 않는다."""
    st = usage_state
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    st.note_active_join_code("ZZ99XX")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    assert st.db.pending_usage_sessions()[0].join_code == JOIN_CODE


def test_auth_success_records_join_code(tmp_path):
    """복합 Bearer 인증이 통과하면 그 라이브가 사용량 귀속 기준이 된다."""

    class OkVerifier(LiveSendVerifier):
        async def _call_verify(self, join_code: str, send_password: str) -> bool:
            return True

    st = _make_state(tmp_path)
    st.live_verifier = OkVerifier("https://ev211.test", "secret")
    with TestClient(create_app(state=st), base_url="https://testserver") as c:
        r = c.post(
            "/channels",
            json={"channel_id": 1, "language": "en", "label": "통역"},
            headers={"Authorization": f"Bearer {JOIN_CODE}:483920"},
        )
        assert r.status_code == 201, r.text
    assert st.active_join_code == JOIN_CODE
    st.db.close()


def test_global_password_does_not_set_join_code(tmp_path):
    """전역 비번(내부망 호환) 인증은 라이브를 특정하지 못하므로 귀속을 만들지 않는다."""
    st = _make_state(tmp_path, mode="both")
    with TestClient(create_app(state=st), base_url="https://testserver") as c:
        r = c.post(
            "/channels",
            json={"channel_id": 1, "language": "en", "label": "통역"},
            headers={"Authorization": f"Bearer {SEND_PW}"},
        )
        assert r.status_code == 201, r.text
    assert st.active_join_code == ""
    st.db.close()


# ---- 세션 수명 ----

def test_duplicate_join_does_not_open_second_session(usage_state):
    st = usage_state
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    assert len(st.db.pending_usage_sessions()) == 1


def test_channel_close_ends_open_sessions(usage_state):
    """이탈 웹훅을 기다리지 않고 채널 종료로 세션을 마감한다(idle 종료·강제 정리 경로)."""
    st = usage_state
    st.db.create_channel(1, "en", "AI: en", source="ai", target_language="en")
    _join(st, direction="send", scope="relay", channel_id=1, subject="speaker-ch-01-e1-g1-nAbc")
    assert st.db.pending_usage_sessions() == []  # 아직 열려 있음

    st.db.close_channel(1)
    pending = st.db.pending_usage_sessions()
    assert len(pending) == 1 and pending[0].ended_at is not None


def test_stale_session_is_capped_not_inflated(usage_state):
    """좀비 세션은 상한까지만 쓴 것으로 마감한다(과대 청구 방지)."""
    st = usage_state
    st.db.create_channel(0, "ko", "원어")
    sid = st.db.open_usage_session(
        join_code=JOIN_CODE, kind="send", subject_hash="deadbeef",
        channel_id=0, started_at=1_000.0,
    )
    assert sid is not None

    closed = st.db.close_stale_usage_sessions(3_600.0, now=1_000.0 + 10_000.0)
    assert closed == 1
    row = st.db.pending_usage_sessions()[0]
    assert row.ended_at == pytest.approx(1_000.0 + 3_600.0)
    assert session_to_event(row)["seconds"] == 3_600


# ---- 보고(push) ----

def test_flush_marks_pushed_and_does_not_resend(usage_state):
    st = usage_state
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    pushed, giveup = _run(st.usage_reporter.flush(st.db))
    assert (pushed, giveup) == (1, 0)
    assert st.db.pending_usage_sessions() == []

    # 두 번째 flush 는 보낼 것이 없다(중복 청구 방지).
    assert _run(st.usage_reporter.flush(st.db)) == (0, 0)
    assert len(st.usage_reporter.batches) == 1


def test_failed_push_is_retried_next_sweep(usage_state):
    st = usage_state
    st.usage_reporter = FakeReporter(ok=False)
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    assert _run(st.usage_reporter.flush(st.db)) == (0, 0)
    pending = st.db.pending_usage_sessions()
    assert len(pending) == 1 and pending[0].attempts == 1  # 미보고로 남아 재시도 대상

    st.usage_reporter.ok = True
    assert _run(st.usage_reporter.flush(st.db))[0] == 1
    assert st.db.pending_usage_sessions() == []


def test_push_gives_up_after_attempt_limit(usage_state):
    st = usage_state
    st.usage_reporter = FakeReporter(ok=False)
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    for _ in range(MAX_ATTEMPTS):
        _run(st.usage_reporter.flush(st.db))
    # 상한 도달 — 다음 flush 에서 포기 처리되고 대기열에서 빠진다(무한 재시도 방지).
    assert _run(st.usage_reporter.flush(st.db)) == (0, 1)
    assert st.db.pending_usage_sessions() == []


def test_disabled_reporter_keeps_sessions_local(tmp_path):
    """내부망 배포(콜백 대상 없음)는 보고하지 않고 로컬에만 남긴다."""
    st = _make_state(tmp_path)
    st.usage_reporter = UsageReporter("", "")
    st.note_active_join_code(JOIN_CODE)
    st.db.create_channel(0, "ko", "원어")
    _join(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")
    _leave(st, direction="send", scope="relay", channel_id=0, subject="speaker-ch-00-e1-g1-nAbc")

    assert st.usage_reporter.enabled is False
    assert _run(st.usage_reporter.flush(st.db)) == (0, 0)
    assert len(st.db.pending_usage_sessions()) == 1
    st.db.close()


def test_sweep_closes_stale_then_pushes(usage_state):
    """스윕 한 번이 좀비 마감 → 보고까지 수행한다."""
    st = usage_state
    st.db.create_channel(0, "ko", "원어")
    st.db.open_usage_session(
        join_code=JOIN_CODE, kind="send", subject_hash="deadbeef",
        channel_id=0, started_at=0.0,
    )
    pushed, _ = _run(_usage_push_sweep(st))
    assert pushed == 1
    assert st.usage_reporter.batches[0][0]["join_code"] == JOIN_CODE


def test_purge_removes_only_reported_sessions(usage_state):
    st = usage_state
    st.db.open_usage_session(
        join_code=JOIN_CODE, kind="send", subject_hash="aaa", started_at=0.0,
    )
    st.db.close_usage_session("aaa", ended_at=1.0)
    st.db.open_usage_session(
        join_code=JOIN_CODE, kind="send", subject_hash="bbb", started_at=0.0,
    )
    st.db.close_usage_session("bbb", ended_at=1.0)
    st.db.mark_usage_sessions_pushed([st.db.pending_usage_sessions()[0].session_id], now=1.0)

    assert st.db.purge_usage_sessions(10.0, now=100.0) == 1
    # 미보고 세션은 보관 기간과 무관하게 남는다(보고 전에 지우면 유실).
    assert len(st.db.pending_usage_sessions()) == 1


# ---- 집계가 통역을 막지 않는다 ----

def test_usage_failure_does_not_break_signal_event(usage_state, monkeypatch):
    st = usage_state

    def boom(*args, **kwargs):
        raise RuntimeError("usage down")

    monkeypatch.setattr(st.db, "open_usage_session", boom)
    st.db.create_channel(0, "ko", "원어")
    assert _join(st, direction="send", scope="relay", channel_id=0,
                 subject="speaker-ch-00-e1-g1-nAbc") is None  # 예외가 새지 않는다
    assert st.db.list_signal_events(limit=5)  # 감사 원장은 정상 기록
