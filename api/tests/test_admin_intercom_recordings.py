# 관리자 무전기 모니터링과 채널별 녹음·MP3 다운로드 API를 검증한다
from __future__ import annotations

import uuid
from pathlib import Path

import jwt
from livekit.protocol.models import ParticipantInfo

from app.recording import RecordingError, RecordingInfo, RecordingManager
from tests.conftest import API_SECRET


class FakeRecordings:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.started = []
        self.stopped = []
        self.active = []
        self.completed = []
        self.fail_duplicate = False

    async def start(self, kind, channel_id, label, room, track_name=None, language=None):
        if self.fail_duplicate:
            raise RecordingError("이 채널은 이미 녹음 중입니다.")
        item = {
            "recording_id": str(uuid.uuid4()),
            "kind": kind,
            "channel_id": channel_id,
            "label": label,
            "room": room,
            "track_name": track_name,
            "language": language,
            "active": True,
        }
        self.started.append(item)
        self.active.append(item)
        return item

    async def stop(self, recording_id):
        if not self.active:
            raise RecordingError("진행 중인 녹음을 찾을 수 없습니다.")
        item = self.active.pop()
        item = {**item, "recording_id": recording_id, "active": False}
        self.stopped.append(recording_id)
        self.completed.append(item)
        return item

    def list(self):
        return {"active": self.active, "recordings": self.completed}

    def download_path(self, recording_id):
        path = self.tmp_path / f"{recording_id}.mp3"
        return path if path.is_file() else None

    def download_filename(self, recording_id):
        return f"ev211-{recording_id}.mp3"

    async def close(self):
        return None


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def test_admin_intercom_status_counts_only_users(client, admin_headers, send_headers, state):
    client.post("/intercom/channels", json={"channel_name": "본부"}, headers=send_headers)
    room = state.intercom_channel_room(0)
    user = ParticipantInfo(identity="intercom-user-1", name="진행자")
    monitor = ParticipantInfo(identity="monitor-dashboard")
    recorder = ParticipantInfo(identity="monitor-recorder-test")
    state.livekit.participants[room] = [user, monitor, recorder]

    assert client.get("/admin/intercom/status").status_code == 401
    response = client.get("/admin/intercom/status", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total_participants"] == 1
    assert body["channels"][0]["participant_count"] == 1
    assert body["channels"][0]["participants"] == [
        {"identity": "intercom-user-1", "name": "진행자"}
    ]


def test_admin_intercom_monitor_token_targets_channel_room(
    client, admin_headers, send_headers, state
):
    client.post("/intercom/channels", json={"channel_name": "무대"}, headers=send_headers)
    response = client.post(
        "/admin/intercom/channels/0/monitor-tokens", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["channel_id"] == 0
    assert body["room"] == state.intercom_channel_room(0)
    video = _decode(body["token"])["video"]
    assert video["room"] == body["room"]
    assert video["hidden"] is True
    assert video["canSubscribe"] is True
    assert not video.get("canPublish", False)


def test_recording_start_stop_list_and_download(
    client, admin_headers, send_headers, state, tmp_path
):
    fake = FakeRecordings(tmp_path)
    state.recordings = fake
    client.post(
        "/channels",
        json={"channel_id": 1, "language": "ko", "label": "한국어"},
        headers=send_headers,
    )

    start = client.post(
        "/admin/recordings",
        json={"kind": "relay", "channel_id": 1},
        headers=admin_headers,
    )
    assert start.status_code == 201
    assert fake.started[0]["track_name"] == "ch-01"
    assert fake.started[0]["room"] == state.room

    listing = client.get("/admin/recordings", headers=admin_headers)
    assert listing.status_code == 200
    assert len(listing.json()["active"]) == 1

    recording_id = start.json()["recording_id"]
    stop = client.post(
        f"/admin/recordings/{recording_id}/stop", headers=admin_headers
    )
    assert stop.status_code == 200
    assert stop.json()["active"] is False

    mp3 = tmp_path / f"{recording_id}.mp3"
    mp3.write_bytes(b"ID3-test")
    download = client.get(
        f"/admin/recordings/{recording_id}/download", headers=admin_headers
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/mpeg")
    assert download.content == b"ID3-test"


def test_intercom_recording_uses_whole_room(
    client, admin_headers, send_headers, state, tmp_path
):
    fake = FakeRecordings(tmp_path)
    state.recordings = fake
    client.post("/intercom/channels", json={"channel_name": "안전팀"}, headers=send_headers)
    response = client.post(
        "/admin/recordings",
        json={"kind": "intercom", "channel_id": 0},
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert fake.started[0]["room"] == state.intercom_channel_room(0)
    assert fake.started[0]["track_name"] is None


def test_recording_rejects_unauthorized_unknown_and_duplicate(
    client, admin_headers, send_headers, state, tmp_path
):
    fake = FakeRecordings(tmp_path)
    state.recordings = fake
    assert client.get("/admin/recordings").status_code == 401
    unknown = client.post(
        "/admin/recordings",
        json={"kind": "relay", "channel_id": 3},
        headers=admin_headers,
    )
    assert unknown.status_code == 404

    client.post(
        "/channels",
        json={"channel_id": 3, "language": "en", "label": "English"},
        headers=send_headers,
    )
    fake.fail_duplicate = True
    duplicate = client.post(
        "/admin/recordings",
        json={"kind": "relay", "channel_id": 3},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409


def test_recording_manager_lists_only_safe_completed_mp3(tmp_path):
    manager = RecordingManager(
        str(tmp_path), "ws://livekit:7880", "key", "secret"
    )
    recording_id = str(uuid.uuid4())
    info = RecordingInfo(
        recording_id=recording_id,
        kind="relay",
        channel_id=1,
        label="한국어",
        room="field-g1",
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
        duration_seconds=60.0,
        size_bytes=3,
    )
    manager._write_metadata(info)
    (tmp_path / f"{recording_id}.mp3").write_bytes(b"ID3")
    (tmp_path / "not-a-uuid.mp3").write_bytes(b"bad")

    listing = manager.list()
    assert [item["recording_id"] for item in listing["recordings"]] == [recording_id]
    assert manager.download_path(recording_id) == tmp_path / f"{recording_id}.mp3"
    assert manager.download_path("../../etc/passwd") is None


# ---- 녹음 삭제 ----
def test_recording_manager_delete_removes_files(tmp_path):
    mgr = RecordingManager(str(tmp_path), "ws://localhost", "k", "s")
    rid = str(uuid.uuid4())
    (tmp_path / f"{rid}.mp3").write_bytes(b"mp3")
    (tmp_path / f"{rid}.json").write_text("{}", encoding="utf-8")
    assert mgr.delete(rid) is True
    assert not (tmp_path / f"{rid}.mp3").exists()
    assert not (tmp_path / f"{rid}.json").exists()
    # 이미 삭제된 ID·잘못된 ID 는 False(404 대응).
    assert mgr.delete(rid) is False
    assert mgr.delete("not-a-uuid") is False


def test_recording_delete_endpoint(client, admin_headers, state, tmp_path):
    mgr = RecordingManager(str(tmp_path), "ws://localhost", "k", "s")
    state.recordings = mgr
    rid = str(uuid.uuid4())
    (tmp_path / f"{rid}.mp3").write_bytes(b"mp3")
    (tmp_path / f"{rid}.json").write_text("{}", encoding="utf-8")

    # 무인증은 거부된다.
    assert client.delete(f"/admin/recordings/{rid}").status_code in (401, 403)

    r = client.delete(f"/admin/recordings/{rid}", headers=admin_headers)
    assert r.status_code == 204
    assert not (tmp_path / f"{rid}.mp3").exists()

    # 없는 녹음은 404.
    r = client.delete(f"/admin/recordings/{rid}", headers=admin_headers)
    assert r.status_code == 404


# ---- 파일명 개선 ----
def test_download_filename_format():
    info = RecordingInfo(
        recording_id="a3f0c1d2-1111-4222-8333-444455556666",
        kind="relay", channel_id=1, label="Korean (한국어)",
        room="r", started_at="2026-07-14T09:30:00+00:00", language="ko",
    )
    assert info.download_filename() == "ch01-ko_20260714_a3f.mp3"


def test_download_filename_intercom_and_label_fallback():
    ic = RecordingInfo(
        recording_id="bcd00000-0000-4000-8000-000000000000",
        kind="intercom", channel_id=3, label="Team A",
        room="r", started_at="2026-07-14T00:00:00+00:00",
    )
    assert ic.download_filename() == "ic03-TeamA_20260714_bcd.mp3"


def test_recording_manager_download_filename(tmp_path):
    import json as _json
    mgr = RecordingManager(str(tmp_path), "ws://localhost", "k", "s")
    rid = "a3f0c1d2-1111-4222-8333-444455556666"
    (tmp_path / f"{rid}.mp3").write_bytes(b"x")
    (tmp_path / f"{rid}.json").write_text(_json.dumps({
        "recording_id": rid, "kind": "relay", "channel_id": 2,
        "label": "Japanese (日本語)", "room": "r",
        "started_at": "2026-07-14T09:00:00+00:00", "language": "ja",
    }), encoding="utf-8")
    assert mgr.download_filename(rid) == "ch02-ja_20260714_a3f.mp3"


# ---- 일괄 삭제 ----
def test_recording_batch_delete(client, admin_headers, state, tmp_path):
    import json as _json
    mgr = RecordingManager(str(tmp_path), "ws://localhost", "k", "s")
    state.recordings = mgr
    ids = []
    for _ in range(3):
        rid = str(uuid.uuid4())
        (tmp_path / f"{rid}.mp3").write_bytes(b"x")
        (tmp_path / f"{rid}.json").write_text(_json.dumps({
            "recording_id": rid, "kind": "relay", "channel_id": 1,
            "label": "ko", "room": "r", "started_at": "2026-07-14T00:00:00+00:00",
        }), encoding="utf-8")
        ids.append(rid)
    missing = str(uuid.uuid4())

    # 무인증 거부.
    assert client.post("/admin/recordings/delete", json={"recording_ids": ids}).status_code in (401, 403)

    r = client.post("/admin/recordings/delete",
                    json={"recording_ids": ids[:2] + [missing]}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_count"] == 2
    assert set(body["deleted"]) == set(ids[:2])
    assert missing in body["skipped"]
    assert not (tmp_path / f"{ids[0]}.mp3").exists()
    assert (tmp_path / f"{ids[2]}.mp3").exists()  # 미선택은 보존
