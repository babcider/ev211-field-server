# 송수신 신호 이벤트 기록과 30일 자동 보존 정책을 검증한다
from __future__ import annotations

import asyncio
import pathlib
import time

from fastapi.testclient import TestClient
from livekit.api import WebhookEvent
from livekit.protocol.models import ParticipantInfo, Room, TrackInfo

from app.config import SIGNAL_LOG_RETENTION_SECONDS
from app.db import new_nonce
from app.main import create_app
from app.webhook import WebhookProcessor


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _event(
    kind: str,
    identity: str,
    event_id: str,
    room: str,
    track_name: str | None = None,
) -> WebhookEvent:
    event = WebhookEvent(event=kind, id=event_id)
    event.room.CopyFrom(Room(name=room))
    event.participant.CopyFrom(ParticipantInfo(identity=identity))
    if track_name is not None:
        event.track.CopyFrom(TrackInfo(name=track_name))
    return event


def test_database_deduplicates_and_purges_events_at_30_day_boundary(state):
    now = 1_800_000_000.0
    cutoff = now - SIGNAL_LOG_RETENTION_SECONDS
    assert state.db.record_signal_event(
        direction="receive",
        event_type="participant_joined",
        scope="relay",
        source_event_id="old",
        occurred_at=cutoff - 0.001,
    )
    assert state.db.record_signal_event(
        direction="send",
        event_type="participant_joined",
        scope="relay",
        source_event_id="boundary",
        occurred_at=cutoff,
    )
    assert state.db.record_signal_event(
        direction="both",
        event_type="participant_joined",
        scope="intercom",
        source_event_id="new",
        occurred_at=now,
    )
    assert not state.db.record_signal_event(
        direction="both",
        event_type="participant_left",
        scope="intercom",
        source_event_id="new",
        occurred_at=now,
    )

    assert state.db.purge_signal_events(SIGNAL_LOG_RETENTION_SECONDS, now=now) == 1
    assert {row.source_event_id for row in state.db.list_signal_events()} == {"boundary", "new"}


def test_successful_token_endpoints_log_receive_send_and_intercom(
    client, state, send_headers, admin_headers
):
    state.db.create_channel(1, "ko", "한국어")

    subscribe = client.post("/channels/1/subscribe-tokens")
    publish = client.post("/publish-tokens", json={"channel_id": 1}, headers=send_headers)
    intercom = client.post("/intercom-tokens", json={"name": "테스트"}, headers=send_headers)
    takeover = client.post("/admin/channels/1/takeover", headers=admin_headers)
    created = client.post(
        "/intercom/channels",
        json={"channel_name": "스태프"},
        headers=send_headers,
    )
    channel_intercom = client.post(
        f"/intercom/channels/{created.json()['channel_id']}/tokens",
        json={"name": "운영"},
        headers=send_headers,
    )

    assert [response.status_code for response in (subscribe, publish, intercom, takeover)] == [
        200,
        200,
        200,
        200,
    ]
    assert created.status_code == 201
    assert channel_intercom.status_code == 200

    rows = state.db.list_signal_events()
    assert len(rows) == 5
    assert {(row.direction, row.scope) for row in rows} == {
        ("receive", "relay"),
        ("send", "relay"),
        ("both", "intercom"),
    }
    assert all(row.event_type == "token_issued" for row in rows)
    assert all(row.subject_hash is not None and len(row.subject_hash) == 16 for row in rows)
    assert all(row.client_ip for row in rows)
    serialized = " ".join(str(value) for row in rows for value in row.__dict__.values())
    assert "send-secret-abc123" not in serialized
    assert "테스트" not in serialized
    assert "운영" not in serialized


def test_webhooks_log_actual_connections_and_ignore_technical_participants(state):
    processor = WebhookProcessor(state)
    state.db.create_channel(1, "ko", "한국어")
    acquired, speaker = state.db.acquire_lease(
        1, 1, state.generation, new_nonce(), 3600
    )
    assert acquired

    events = [
        _event("participant_joined", speaker, "relay-send", state.room),
        _event("participant_joined", "listener-example", "relay-receive", state.room),
        _event("participant_joined", "intercom-example", "intercom-both", "intercom-g1-c3"),
        _event(
            "track_published",
            "intercom-example",
            "intercom-send",
            "intercom-g1-c3",
            "ic-example",
        ),
        _event("participant_joined", "monitor-recorder-example", "technical", state.room),
        _event("participant_joined", "listener-other", "other-room", "unrelated-room"),
    ]
    for event in events:
        _run(processor.handle(event))
    _run(processor.handle(events[0]))

    rows = state.db.list_signal_events()
    assert len(rows) == 4
    by_source = {row.source_event_id: row for row in rows}
    assert (by_source["relay-send"].direction, by_source["relay-send"].channel_id) == (
        "send",
        1,
    )
    assert by_source["relay-receive"].direction == "receive"
    assert (
        by_source["intercom-both"].direction,
        by_source["intercom-both"].scope,
        by_source["intercom-both"].channel_id,
    ) == ("both", "intercom", 3)
    assert (
        by_source["intercom-send"].direction,
        by_source["intercom-send"].track_name,
    ) == ("send", "ic-example")
    assert "technical" not in by_source
    assert "other-room" not in by_source


def test_app_startup_purges_expired_signal_events(state):
    state.db.record_signal_event(
        direction="receive",
        event_type="token_issued",
        scope="relay",
        occurred_at=time.time() - SIGNAL_LOG_RETENTION_SECONDS - 1,
    )
    app = create_app(state=state)
    with TestClient(app, base_url="https://testserver"):
        assert state.db.list_signal_events() == []


def test_update_installs_current_management_cli_and_restores_it_on_rollback():
    script = (pathlib.Path(__file__).parents[2] / "scripts" / "ev211ctl").read_text()
    pull = 'git -C "$APP_DIR" pull --ff-only'
    install_cli = 'install -m 755 "$APP_DIR/scripts/ev211ctl" /usr/local/bin/ev211ctl'
    rollback = 'git -C "$APP_DIR" reset --hard "$before"'

    assert script.index(pull) < script.index(install_cli)
    assert script.index(rollback) < script.rindex(install_cli)
