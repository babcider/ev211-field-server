# LiveKit RoomService 래퍼 — 룸 생성/삭제·participant 제거·목록 조회를 비동기로 감싼다
from __future__ import annotations

from livekit import api


class LiveKitClient:
    """LiveKit HTTP(RoomService) API 를 감싸는 얇은 래퍼.

    테스트에서는 이 클래스를 mock 으로 대체한다(계약: LiveKit API 는 mock).
    """

    def __init__(self, host: str, api_key: str, api_secret: str) -> None:
        self._host = host
        self._api_key = api_key
        self._api_secret = api_secret

    def _client(self) -> api.LiveKitAPI:
        return api.LiveKitAPI(self._host, self._api_key, self._api_secret)

    async def list_rooms(self) -> list[str]:
        client = self._client()
        try:
            resp = await client.room.list_rooms(api.ListRoomsRequest())
            return [r.name for r in resp.rooms]
        finally:
            await client.aclose()

    async def create_room(
        self, name: str, empty_timeout: int = 86400, max_participants: int = 0
    ) -> None:
        # empty_timeout 기본값을 크게(24h) 둬 빈 룸이 자동 삭제되지 않도록 한다(#3).
        # max_participants>0 이면 LiveKit 이 초과 접속을 서버측에서 거부한다(인터컴 상한).
        client = self._client()
        try:
            await client.room.create_room(
                api.CreateRoomRequest(
                    name=name, empty_timeout=empty_timeout, max_participants=max_participants
                )
            )
        finally:
            await client.aclose()

    async def delete_room(self, name: str) -> None:
        client = self._client()
        try:
            await client.room.delete_room(api.DeleteRoomRequest(room=name))
        finally:
            await client.aclose()

    async def remove_participant(self, room: str, identity: str) -> None:
        client = self._client()
        try:
            await client.room.remove_participant(
                api.RoomParticipantIdentity(room=room, identity=identity)
            )
        finally:
            await client.aclose()

    async def list_participants(self, room: str) -> list:
        client = self._client()
        try:
            resp = await client.room.list_participants(
                api.ListParticipantsRequest(room=room)
            )
            return list(resp.participants)
        finally:
            await client.aclose()

    async def connected(self) -> bool:
        """LiveKit HTTP API 연결 가능 여부(헬스체크용)."""
        try:
            await self.list_rooms()
            return True
        except Exception:
            return False
