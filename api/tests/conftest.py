# pytest 픽스처 — mock LiveKit 클라이언트로 테스트용 AppState·TestClient 를 구성한다
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.state import AppState  # noqa: E402

SEND_PW = "send-secret-abc123"
ADMIN_PW = "admin-secret-xyz789"
API_KEY = "field_test_key"
API_SECRET = "unit-test-placeholder-" + ("x" * 32)


class MockLiveKit:
    """RoomService 호출을 기록하는 mock. 실제 네트워크 없음."""

    def __init__(self) -> None:
        self.rooms: list[str] = []
        self.removed: list[tuple[str, str]] = []
        self.participants: dict[str, list] = {}
        self.room_caps: dict[str, int] = {}  # 룸별 max_participants 기록(상한 검증용)
        self._connected = True

    async def list_rooms(self) -> list[str]:
        return list(self.rooms)

    async def create_room(
        self, name: str, empty_timeout: int = 86400, max_participants: int = 0
    ) -> None:
        if name not in self.rooms:
            self.rooms.append(name)
        self.room_caps[name] = max_participants

    async def delete_room(self, name: str) -> None:
        if name in self.rooms:
            self.rooms.remove(name)

    async def remove_participant(self, room: str, identity: str) -> None:
        self.removed.append((room, identity))

    async def list_participants(self, room: str) -> list:
        return self.participants.get(room, [])

    async def connected(self) -> bool:
        return self._connected


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        livekit_api_key=API_KEY,
        livekit_api_secret=API_SECRET,
        livekit_host="http://livekit:7880",
        livekit_rtc_url="ws://livekit:7880",
        ws_url="ws://192.168.0.10:7880",
        send_password=SEND_PW,
        admin_password=ADMIN_PW,
        db_path=str(tmp_path / "field.db"),
        recordings_path=str(tmp_path / "recordings"),
        max_channels=15,
        forwarded_allow_ips="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )


@pytest.fixture
def state(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    lk = MockLiveKit()
    st = AppState(settings, db, lk)
    # bootstrap 을 동기적으로 실행(세대 결정·룸 생성).
    import asyncio

    asyncio.get_event_loop().run_until_complete(st.bootstrap())
    yield st
    db.close()


@pytest.fixture
def client(state):
    # 인증 엔드포인트는 https 강제이므로 기본 base_url 을 https 로 둔다(TestClient 는
    # 이 scheme 을 request.url.scheme 으로 전달한다). http 강제 거부는 별도 테스트에서 확인.
    app = create_app(state=state)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def http_client(state):
    """http scheme 클라이언트 — https 강제(403) 검증 전용."""
    app = create_app(state=state)
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def send_headers():
    return {"Authorization": f"Bearer {SEND_PW}"}


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {ADMIN_PW}"}
