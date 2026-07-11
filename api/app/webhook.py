# LiveKit 웹훅 서명 검증·강제 로직 — participant_joined 즉시 검증, track 1개 제한, stale left 방어
from __future__ import annotations

from livekit.api import TokenVerifier, WebhookReceiver

from .identity import is_intercom_identity, is_monitor_identity, parse_speaker_identity
from .state import AppState


class WebhookProcessor:
    """서명 검증된 LiveKit 웹훅을 받아 계약의 서버측 강제 규칙을 적용한다."""

    def __init__(self, state: AppState) -> None:
        self._state = state
        verifier = TokenVerifier(state.settings.livekit_api_key, state.settings.livekit_api_secret)
        self._receiver = WebhookReceiver(verifier)

    def verify(self, body: str, auth_token: str):
        """Authorization JWT 서명·페이로드 해시를 검증하고 WebhookEvent 를 반환한다.

        서명 불일치·만료 시 예외를 던진다(호출부에서 401 처리 후 폐기).
        """
        return self._receiver.receive(body, auth_token)

    async def handle(self, event) -> None:
        """검증된 이벤트를 멱등·순서 방어와 함께 처리한다(#1 처리 성공 후 커밋).

        순서: ① 멱등 선-확인(읽기 전용) — 이미 커밋된 id 면 재처리하지 않고 반환.
        ② 이벤트 처리(RemoveParticipant·lease 반영 등) 실행. 처리 중 예외는 삼키지
        않고 그대로 전파해 호출부(main)가 event id 를 **커밋하지 않고** 503 을 반환하게
        한다(LiveKit 재전송 유도). ③ 처리가 예외 없이 끝난 경우에만 event id 를 커밋한다.
        """
        state = self._state
        event_id = event.id
        # ① 멱등 선-확인: 이미 처리 성공해 커밋된 id 는 재처리하지 않는다.
        if event_id and state.db.is_webhook_processed(event_id):
            return

        # 룸 스코프(Phase 2c): 강제 규칙(identity 규약·트랙명·lease·on-air)은 현재
        # 세대 릴레이 룸(field-g<gen>) 전용이다. 인터컴 룸(intercom-g*) 등 다른 룸의
        # 이벤트는 처리 없이 멱등 커밋만 한다(릴레이 상태를 오염시키지 않게).
        # room 정보가 없는 이벤트(빈 이름)는 종전대로 identity 기반 처리로 흘린다.
        room = getattr(event, "room", None)
        event_room = getattr(room, "name", "") if room is not None else ""
        if event_room and event_room != state.room:
            if event_id:
                state.db.mark_webhook_processed(event_id)
            return

        # ② 이벤트 처리 — 실패 시 예외를 전파(커밋 없이 503 → 재전송).
        kind = event.event
        if kind == "participant_joined":
            await self._on_joined(event)
        elif kind == "track_published":
            await self._on_track_published(event)
        elif kind == "track_unpublished":
            await self._on_track_unpublished(event)
        elif kind == "participant_left":
            await self._on_left(event)
        # 그 외 이벤트(room_started 등)는 무시(처리 성공으로 간주).

        # ③ 처리 성공 — 이제서야 event id 를 멱등 커밋한다.
        if event_id:
            state.db.mark_webhook_processed(event_id)

    async def _remove(self, identity: str) -> None:
        """participant 제거를 시도한다(#1).

        이미 룸에 없어 발생하는 not-found 류 오류는 목표 상태(제거됨)와 동일하므로
        성공으로 간주해 삼킨다. 그 외(LiveKit 연결 실패 등)는 예외를 전파해 handle 이
        event id 를 커밋하지 못하게 하고 503 재전송을 유도한다.
        """
        try:
            await self._state.livekit.remove_participant(self._state.room, identity)
        except Exception as exc:  # noqa: BLE001 — 메시지로 not-found 여부만 구분
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg or "no participant" in msg:
                return
            raise

    async def _on_joined(self, event) -> None:
        state = self._state
        p = event.participant
        if p is None:
            return
        identity = p.identity
        if not identity or is_monitor_identity(identity) or is_intercom_identity(identity):
            return  # 모니터·인터컴은 강제 대상에서 제외(룸 스코프 가드의 이중 방어).

        # 차단 목록: 반복 위반 identity 는 발행 이전에 즉시 제거.
        if state.blocklist.is_blocked(identity):
            await self._remove(identity)
            return

        parsed = parse_speaker_identity(identity)
        if parsed is None:
            # listener-* 등 비송신 identity 는 통과, 그 외 미규약 identity 는 위반.
            if identity.startswith("listener-"):
                return
            state.blocklist.strike(identity)
            await self._remove(identity)
            return

        channel = state.db.get_channel(parsed.channel_id)
        # generation 불일치·구 epoch·채널 없음·lease identity 불일치면 즉시 제거.
        lease = state.db.get_lease(parsed.channel_id)
        violation = (
            channel is None
            or parsed.generation != state.generation
            or (parsed.epoch < channel.epoch)
            or lease is None
            or lease.identity != identity
        )
        if violation:
            state.blocklist.strike(identity)
            await self._remove(identity)
            return

        # 정상 접속: lease 에 접속 시각 기록.
        state.db.mark_lease_joined(parsed.channel_id, identity)

    async def _on_track_published(self, event) -> None:
        state = self._state
        p = event.participant
        track = event.track
        if p is None:
            return
        identity = p.identity
        if not identity or is_monitor_identity(identity) or is_intercom_identity(identity):
            return
        parsed = parse_speaker_identity(identity)
        if parsed is None:
            # 규약 위반 identity 의 발행 시도 → 제거(#5, listener 등 비송신 우회 차단).
            if not identity.startswith("listener-"):
                state.blocklist.strike(identity)
                await self._remove(identity)
            return

        expected_track = f"ch-{parsed.channel_id:02d}"

        # (1) lease 재검증(#5): 채널 open + 현재 lease identity 전체 문자열 일치.
        channel = state.db.get_channel(parsed.channel_id)
        lease = state.db.get_lease(parsed.channel_id)
        if (
            channel is None
            or channel.state != "open"
            or parsed.generation != state.generation
            or parsed.epoch < channel.epoch
            or lease is None
            or lease.identity != identity
        ):
            state.blocklist.strike(identity)
            await self._remove(identity)
            return

        # (2) 트랙명 규약(#5): 빈 이름 거부 + identity 채널과 일치(ch-NN)해야 함.
        track_name = track.name if track is not None else None
        if not track_name or track_name != expected_track:
            state.blocklist.strike(identity)
            await self._remove(identity)
            return

        # (3) participant당 발행 트랙 1개 제한 — 이미 발행 중인데 추가 발행이면 초과로 제거.
        try:
            participants = await state.livekit.list_participants(state.room)
        except Exception:
            participants = []
        for part in participants:
            if part.identity == identity and len(part.tracks) > 1:
                state.blocklist.strike(identity)
                await self._remove(identity)
                return

        # 정상 — on-air 근사 상태를 발행(송신) 중으로 표시.
        state.on_air.set_publishing(parsed.channel_id, True)

    async def _on_track_unpublished(self, event) -> None:
        """트랙 발행 종료 시 on-air(발행 중) 근사 상태를 해제한다(#16).

        #5 stale 방어: 떠난 트랙의 identity 가 **현재 lease identity 와 정확히 일치할
        때만** 해제한다. takeover/재발급으로 새 송신자가 이미 발행 중이면, 구 송신자의
        지연 도착한 track_unpublished 가 새 on-air 상태를 지우지 못하게 한다.
        """
        state = self._state
        p = event.participant
        if p is None:
            return
        identity = p.identity
        if not identity or is_monitor_identity(identity):
            return
        parsed = parse_speaker_identity(identity)
        if parsed is None:
            return
        lease = state.db.get_lease(parsed.channel_id)
        if lease is None or lease.identity != identity:
            return  # stale — 현재 lease 소유자가 아니면 on-air 를 건드리지 않는다.
        state.on_air.set_publishing(parsed.channel_id, False)

    async def _on_left(self, event) -> None:
        state = self._state
        p = event.participant
        if p is None:
            return
        identity = p.identity
        if not identity or is_monitor_identity(identity):
            return
        parsed = parse_speaker_identity(identity)
        if parsed is None:
            return
        # stale left 방어: 현재 lease identity 와 전체 문자열 정확 일치 시에만 해제.
        released = state.db.release_lease_if_identity(parsed.channel_id, identity)
        if released:
            state.on_air.clear_channel(parsed.channel_id)
