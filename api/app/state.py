# 앱 전역 상태 컨테이너 — 설정·DB·LiveKit 클라이언트·rate limiter·on-air 트래커를 묶는다
from __future__ import annotations

import asyncio
import hashlib
import logging

from .config import (
    ADMIN_FAIL_LIMIT,
    ADMIN_LOCK_SECONDS,
    ADMIN_RL_PER_MINUTE,
    HEARTBEAT_RL_PER_MINUTE,
    PUBLISH_FAIL_LIMIT,
    PUBLISH_LOCK_SECONDS,
    PUBLISH_RL_PER_MINUTE,
    PUBLISH_TTL_SECONDS,
    ROOM_EMPTY_TIMEOUT_SECONDS,
    SUBSCRIBE_RL_PER_MINUTE,
    Settings,
)
from .live_auth import LiveSendVerifier
from .db import Database, new_nonce
from .identity import parse_speaker_identity
from .livekit_client import LiveKitClient
from .monitor import OnAirTracker
from .rate_limit import Blocklist, FailureLock, RateLimiter
from .recording import RecordingManager
from .tokens import (
    intercom_channel_room_name,
    intercom_room_name,
    issue_publish_token,
    room_name,
)
from .translate_worker import (
    AiChannelManager,
    AiWorkerParams,
    build_default_ai_worker_factory,
)

log = logging.getLogger(__name__)


def password_hash(send_pw: str, admin_pw: str) -> str:
    """두 비밀번호를 함께 해싱해 세대 변경 감지에 쓴다(둘 중 하나만 바뀌어도 세대 증가)."""
    return hashlib.sha256(f"{send_pw}\x00{admin_pw}".encode()).hexdigest()


class AppState:
    def __init__(self, settings: Settings, db: Database, livekit: LiveKitClient) -> None:
        self.settings = settings
        self.db = db
        self.livekit = livekit
        self.on_air = OnAirTracker()
        self.recordings = RecordingManager(
            settings.recordings_path,
            settings.livekit_rtc_url,
            settings.livekit_api_key,
            settings.livekit_api_secret,
        )

        self.subscribe_rl = RateLimiter(SUBSCRIBE_RL_PER_MINUTE)
        self.heartbeat_rl = RateLimiter(HEARTBEAT_RL_PER_MINUTE)
        self.publish_rl = RateLimiter(PUBLISH_RL_PER_MINUTE)
        self.admin_rl = RateLimiter(ADMIN_RL_PER_MINUTE)
        self.publish_lock = FailureLock(PUBLISH_FAIL_LIMIT, PUBLISH_LOCK_SECONDS)
        self.admin_lock = FailureLock(ADMIN_FAIL_LIMIT, ADMIN_LOCK_SECONDS)
        # 무전기 채널 비밀번호 전용 실패 잠금(22차): 송신 비번 성공과 무관하게 채널 비번
        # 대입을 (IP, channel_id) 키로 잠근다 — 4자리 비번 온라인 대입 방어.
        self.intercom_pw_lock = FailureLock(PUBLISH_FAIL_LIMIT, PUBLISH_LOCK_SECONDS)
        self.blocklist = Blocklist()

        self.generation: int = 1

        # 런타임 비밀번호 — 앱(관리자 메뉴)에서 변경하면 DB settings 에 영속되고,
        # 여기 값이 갱신된다. 초기값은 DB 오버라이드 우선, 없으면 환경변수(.env).
        self.send_password: str = db.get_setting("send_password") or settings.send_password
        self.admin_password: str = db.get_setting("admin_password") or settings.admin_password

        # 라이브별 송신 비번 콜백 검증기(계약 §7) — callback|both 모드에서만 생성.
        # 테스트에서는 이 속성을 Fake 로 교체한다.
        self.live_verifier: LiveSendVerifier | None = (
            LiveSendVerifier(settings.ev211_api_base, settings.field_callback_secret)
            if settings.send_auth_mode in ("callback", "both")
            else None
        )

        # 채널별 asyncio.Lock — close/takeover/publish-token 발급을 채널 단위로 직렬화한다(#2).
        # RemoveParticipant await 와 DB 상태 전이(lease 해제·epoch 증가·재획득) 사이에
        # 다른 코루틴의 새 lease 발급이 끼어들지 못하게 한다. 락 획득 순서는 "채널 락 1개"
        # 뿐이므로(중첩 획득 없음) 데드락이 생기지 않는다.
        self._channel_locks: dict[int, asyncio.Lock] = {}

        # 자격증명 회전 전역 락(13차 #2) — 두 회전이 같은 new_gen 을 계산하거나,
        # 관리자·송신자 비밀번호가 동시 변경으로 같은 값이 되는 경쟁을 직렬화로 막는다.
        self._rotation_lock = asyncio.Lock()

        # 무전기 채널 비번 검증 직렬화·자원 상한(23차):
        # - per-key(IP:channel) asyncio.Lock 으로 is_locked~검증~record 를 원자화해
        #   동시 요청이 5회 제한을 초과하지 못하게 한다(22차 #1 강화).
        # - scrypt 는 이벤트루프를 막지 않도록 to_thread 로 돌리되, 세마포어로 동시
        #   실행 수를 제한해 다IP 동시 요청의 CPU/메모리 폭주를 막는다(23차 신규).
        self._intercom_pw_key_locks: dict[str, asyncio.Lock] = {}
        self.intercom_scrypt_sem = asyncio.Semaphore(2)

        # 모드 C AI 통역 채널 슈퍼바이저 레지스트리. 운영 기본 워커 팩토리(실 OpenAI WS +
        # rtc.Room)를 주입한다. 테스트는 이 속성을 fake 워커 팩토리 매니저로 교체한다.
        self.ai_channels = AiChannelManager(build_default_ai_worker_factory(settings))

    def intercom_pw_key_lock(self, key: str) -> asyncio.Lock:
        """채널 비번 시도(IP:channel_id) 키별 직렬화 락(없으면 생성)."""
        lock = self._intercom_pw_key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._intercom_pw_key_locks[key] = lock
        return lock

    def record_signal_event(
        self,
        *,
        direction: str,
        event_type: str,
        scope: str,
        channel_id: int | None = None,
        generation: int | None = None,
        room: str | None = None,
        track_name: str | None = None,
        subject: str | None = None,
        client_ip: str | None = None,
        source_event_id: str | None = None,
    ) -> bool:
        """민감한 원문 식별자를 해시한 뒤 송수신 세션 이벤트를 영속한다."""
        subject_hash = None
        if subject:
            subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
        inserted = self.db.record_signal_event(
            direction=direction,
            event_type=event_type,
            scope=scope,
            channel_id=channel_id,
            generation=self.generation if generation is None else generation,
            room=room[:128] if room else None,
            track_name=track_name[:128] if track_name else None,
            subject_hash=subject_hash,
            client_ip=client_ip[:64] if client_ip else None,
            source_event_id=source_event_id[:128] if source_event_id else None,
        )
        if inserted:
            log.info(
                "signal_event direction=%s event=%s scope=%s channel=%s generation=%s",
                direction,
                event_type,
                scope,
                channel_id,
                self.generation if generation is None else generation,
            )
        return inserted

    def channel_lock(self, channel_id: int) -> asyncio.Lock:
        """채널별 직렬화 락을 반환한다(없으면 생성). 이벤트루프 단일 스레드에서만 호출된다.

        락 사용 규약(#2): close/takeover/publish-token 발급의 임계 구역 전체를 이 락으로
        감싼다. 한 코루틴은 채널 락을 **하나만** 잡고, 그 안에서 다른 채널 락이나 다른
        전역 락을 잡지 않으므로 락 순환이 없다(데드락 불가).
        """
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    @property
    def room(self) -> str:
        return room_name(self.generation)

    async def ensure_room(self) -> None:
        """현재 세대 룸 존재를 보장한다(없으면 재생성).

        empty_timeout·재시작 등으로 룸이 사라지면 이후 발급 토큰이 join 실패하므로,
        모든 토큰 발급 경로에서 발급 직전에 호출한다. create_room 은 이미 존재하면
        멱등(LiveKit 는 동일 이름 재생성을 무해 처리)이다.
        """
        try:
            existing = await self.livekit.list_rooms()
        except Exception:
            existing = []
        if self.room not in existing:
            await self.livekit.create_room(self.room, empty_timeout=ROOM_EMPTY_TIMEOUT_SECONDS)

    @property
    def intercom_room(self) -> str:
        return intercom_room_name(self.generation)

    async def ensure_intercom_room(self) -> None:
        """현재 세대 인터컴 룸 존재를 보장한다(없으면 생성 — 발급 시 lazy).

        릴레이 룸(ensure_room)과 동일 규약: create_room 은 멱등이고, 조회 실패 시
        생성 시도로 폴백한다. 생성 실패는 예외를 전파해 호출부가 502 로 보상한다.
        max_participants 로 LiveKit 이 참가 상한을 서버측 강제한다(17차 #3 —
        발급 시점 계수만으로는 미접속 토큰 다발 발급으로 상한이 우회되므로).
        """
        try:
            existing = await self.livekit.list_rooms()
        except Exception:
            existing = []
        if self.intercom_room not in existing:
            await self.livekit.create_room(
                self.intercom_room,
                empty_timeout=ROOM_EMPTY_TIMEOUT_SECONDS,
                # 사용자 상한(settings.intercom_max) + 관리자 모니터 1명 + 서버 녹음 1명.
                max_participants=self.settings.intercom_max + 2,
            )

    def intercom_channel_room(self, channel_id: int) -> str:
        return intercom_channel_room_name(self.generation, channel_id)

    async def ensure_intercom_channel_room(self, channel_id: int) -> None:
        """무전기 채널 룸(intercom-g<gen>-c<id>) 존재를 보장한다(발급 시 lazy 생성).

        기본 인터컴 룸과 동일하게 max_participants 로 참가 상한을 서버측 강제한다.
        """
        room = self.intercom_channel_room(channel_id)
        try:
            existing = await self.livekit.list_rooms()
        except Exception:
            existing = []
        if room not in existing:
            await self.livekit.create_room(
                room,
                empty_timeout=ROOM_EMPTY_TIMEOUT_SECONDS,
                # 사용자 상한(settings.intercom_max) + 관리자 모니터 1명 + 서버 녹음 1명.
                max_participants=self.settings.intercom_max + 2,
            )

    @property
    def rotation_lock(self) -> asyncio.Lock:
        """자격증명 회전 전역 락(13차 #2·14차).

        규약: 비밀번호 회전뿐 아니라 **자격증명이 지키는 임계구역 전체**(publish·
        takeover·close·subscribe 의 인증~ensure_room~상태 전이~토큰 발급)를 이 락으로
        감싸고, 락 획득 후 인증을 재검증한다. 락 순서는 항상 "회전 락 → 채널 락"이며
        역순 획득이 없으므로 데드락이 생기지 않는다.
        """
        return self._rotation_lock

    async def change_admin_password(self, new_password: str) -> None:
        """관리자 비밀번호 변경(회전 락 획득 포함). 테스트·내부용 편의 래퍼."""
        async with self._rotation_lock:
            self.change_admin_password_locked(new_password)

    def change_admin_password_locked(self, new_password: str) -> None:
        """관리자 비밀번호 변경 — 세대는 유지한다(진행 중 방송을 끊지 않음).

        **호출자가 rotation_lock 을 보유한 상태**여야 한다(엔드포인트는 락 획득 후
        인증을 재검증하고 호출한다 — 14차 #2).
        - 동일값 검증부터 커밋까지 await 없이 수행된다(단일 이벤트루프에서 원자적).
        - 비밀번호와 세대 해시를 **단일 트랜잭션**으로 저장해(12차 #5), 중간 실패 시
          비밀번호·해시 불일치로 다음 재시작에서 의도치 않은 세대 회전이 나는 것을 막는다.
        - 메모리 값은 DB 커밋 성공 후에만 갱신한다.
        """
        if new_password == self.send_password:
            raise ValueError("관리자 비밀번호는 송신자 비밀번호와 달라야 합니다.")
        new_hash = password_hash(self.send_password, new_password)
        self.db.set_admin_password_and_generation(new_password, self.generation, new_hash)
        self.admin_password = new_password

    async def change_send_password(self, new_password: str) -> None:
        """송신자 비밀번호 변경(회전 락 획득 포함). 테스트·내부용 편의 래퍼."""
        async with self._rotation_lock:
            await self.change_send_password_locked(new_password)

    async def change_send_password_locked(self, new_password: str) -> None:
        """송신자 비밀번호 변경 — 세대를 회전한다(기존 토큰·룸·lease 전부 폐기, 계약).

        **호출자가 rotation_lock 을 보유한 상태**여야 한다(14차 #2).
        - 실패 원자성(12차 #1): 실패할 수 있는 LiveKit 작업(새 세대 룸 생성·구세대 룸
          삭제)을 **먼저** 수행하고, 전부 성공한 뒤에만 비밀번호·세대·lease 폐기를
          **단일 DB 트랜잭션**(13차 #4)으로 영속한다. 중간 실패 시 예외를 전파하며
          비밀번호·세대·lease 는 변경 전 그대로 남는다(구세대 룸 일부가 이미 삭제됐을
          수 있으나, 이는 세션 중단일 뿐 보안 저하가 아니다).
        """
        # 동일값 재검증: 동시 관리자 비밀번호 변경으로 같은 값이 되는 경쟁 차단(락 보유 중).
        if new_password == self.admin_password:
            raise ValueError("송신자 비밀번호는 관리자 비밀번호와 달라야 합니다.")
        new_gen = self.generation + 1
        new_room = room_name(new_gen)

        # ① 실패 가능 구간: LiveKit 룸 회전. 여기서 던지면 아무 것도 영속되지 않는다.
        # 인터컴 룸(intercom-g*)도 함께 폐기한다 — 구세대 인터컴 토큰 무효화(새 세대
        # 인터컴 룸은 발급 시 lazy 생성).
        await self.livekit.create_room(new_room, empty_timeout=ROOM_EMPTY_TIMEOUT_SECONDS)
        existing_rooms = await self.livekit.list_rooms()
        new_intercom = intercom_room_name(new_gen)
        for name in existing_rooms:
            if name.startswith("field-g") and name != new_room:
                await self.livekit.delete_room(name)
            elif name.startswith("intercom-g") and name != new_intercom:
                await self.livekit.delete_room(name)

        # ② 영속 구간: 비밀번호·세대·lease 폐기를 단일 트랜잭션으로 커밋한 뒤
        #    메모리 상태를 갱신한다.
        new_hash = password_hash(new_password, self.admin_password)
        # rotate_send_password 트랜잭션이 채널 메타데이터 폐기까지 원자적으로 처리한다(22차).
        self.db.rotate_send_password(new_password, new_gen, new_hash)
        self.send_password = new_password
        self.generation = new_gen
        self.blocklist.clear()
        self.on_air.clear()
        # 세대 회전으로 구세대 룸·토큰이 폐기되므로 AI 통역 워커도 전부 정지한다
        # (구세대 토큰으로는 새 룸에 접속할 수 없어 무한 재시작하게 되기 때문).
        await self.ai_channels.close()
        # 인메모리 워커를 정지해도 open AI 채널 행은 DB 에 남아 슬롯을 점유하므로 함께
        # 닫는다(HIGH2). rotate_send_password 가 이미 전체 lease 를 폐기했다.
        closed_ai = self.db.close_open_ai_channels()
        if closed_ai:
            log.info("closed_orphan_ai_channels_on_rotation channels=%s", closed_ai)

    async def bootstrap(self) -> None:
        """기동 시퀀스: 세대 결정 → 새 세대 룸 생성 → 구세대 룸 삭제 → reconcile."""
        new_hash = password_hash(self.send_password, self.admin_password)
        current = self.db.get_generation()
        generation_changed = False
        if current is None:
            self.generation = 1
        else:
            gen_value, stored_hash = current
            if stored_hash != new_hash:
                self.generation = gen_value + 1
                generation_changed = True
            else:
                self.generation = gen_value
        self.db.set_generation(self.generation, new_hash)

        # 세대 변경 시 구세대 lease 를 전부 정리한다(구세대 토큰은 어차피 무효).
        if generation_changed:
            self.db.clear_all_leases()

        # 이전 프로세스가 남긴 open AI 채널 행은 인메모리 워커가 없는 상태다. 세대가
        # 바뀌었거나(구세대 토큰 무효) OpenAI 키가 없으면 복원할 수 없으므로 슬롯을
        # 영구 점유하지 않도록 원자적으로 닫는다(HIGH2). 그 외에는 reconcile 이 끝난 뒤
        # 워커를 다시 스폰해 복원한다(bootstrap 말미의 restore_ai_channels).
        if generation_changed or not self.settings.openai_api_key:
            closed_ai = self.db.close_open_ai_channels()
            if closed_ai:
                log.info("closed_orphan_ai_channels_on_bootstrap channels=%s", closed_ai)

        # 세대 변경 시 메모리 차단 목록·on-air 초기화(계약).
        self.blocklist.clear()
        self.on_air.clear()

        # LiveKit reconcile: 새 세대 룸 생성, 구세대 field-g* 룸 삭제.
        # 조회 실패를 빈 목록으로 간주하면 구세대 룸 삭제를 건너뛰어 폐기된 세대의
        # JWT 가 계속 유효할 수 있으므로, 실패 시 예외를 전파해 기동을 중단한다(fail-fast).
        existing_rooms = await self.livekit.list_rooms()

        target = self.room
        if target not in existing_rooms:
            await self.livekit.create_room(target, empty_timeout=ROOM_EMPTY_TIMEOUT_SECONDS)
        # 구세대 룸 삭제 — 실패 시 폐기된 세대 토큰으로 룸이 되살아날 수 있으므로 기동 실패(fail-fast).
        # 인터컴 룸은 **세대 무관 전부** 삭제한다(18차 #3): 인터컴은 상시 상태가 없고,
        # max_participants 상한이 없는 구 빌드·수동 생성 룸이 잔존하면 상한이 우회되므로
        # 재시작 시 폐기 후 발급 경로에서 상한과 함께 lazy 재생성한다.
        for name in existing_rooms:
            if name.startswith("field-g") and name != target:
                await self.livekit.delete_room(name)
            elif name.startswith("intercom-g"):
                await self.livekit.delete_room(name)
        # 인터컴 룸을 전부 폐기했으므로 채널 메타데이터도 초기화한다(룸·메타 정합).
        self.db.clear_intercom_channels()

        # reconcile(#5·#6①·#16): 재시작 후 현재 룸의 실제 참가자와 DB lease 를 대조한다.
        # ListParticipants 조회 실패를 빈 룸으로 간주하면 실존 발행자를 놓쳐 고아 lease 를
        # 방치하거나 활성 송신을 오판하므로, 조회 실패 시 예외를 전파해 기동을 중단한다(fail-fast).
        participants = await self.livekit.list_participants(target)

        # 룸에 실존하는 규약 speaker identity 집합(집합으로 O(1) 조회).
        live_speaker_idents = {
            getattr(p, "identity", None)
            for p in participants
            if getattr(p, "identity", None) and parse_speaker_identity(getattr(p, "identity", ""))
        }

        # #6①: LiveKit 재시작 등으로 룸에 실존하지 않는 joined lease 를 해제한다
        # (고착된 joined lease 가 후속 publish 를 영구 409 로 막지 않도록).
        for ch in self.db.list_channels():
            lease = self.db.get_lease(ch.channel_id)
            if lease is None or lease.joined_at is None:
                continue  # 미접속 lease 는 grace 로직에 맡긴다(여기서 손대지 않음).
            if lease.identity not in live_speaker_idents:
                self.db.release_lease_if_identity(ch.channel_id, lease.identity)

        # #5·#16: 현재 룸의 발행 중 speaker 를 lease 와 대조한다.
        # - lease identity 와 일치하고 트랙을 발행 중이면 on-air 복원(웹훅 유실 대비).
        # - 대응 lease 가 없는(고아) 발행자는 즉시 제거한다.
        for part in participants:
            ident = getattr(part, "identity", None)
            if not ident:
                continue
            parsed = parse_speaker_identity(ident)
            if parsed is None:
                continue
            lease = self.db.get_lease(parsed.channel_id)
            if lease is not None and lease.identity == ident:
                if getattr(part, "tracks", None):
                    self.on_air.set_publishing(parsed.channel_id, True)
            else:
                # lease 없는(또는 불일치) 발행자 — 유효하지 않으므로 제거.
                # 제거 실패를 삼키면 고아 발행자가 룸에 남아 후속 lease 발급을 계속 막으므로
                # 예외를 전파해 기동을 중단한다(fail-fast).
                await self.livekit.remove_participant(target, ident)

        # reconcile 로 고아 lease 를 정리한 뒤 AI 통역 워커를 복원한다(순서 중요 —
        # 이전 프로세스의 lease 가 남아 있으면 새 워커 identity 와 어긋난다).
        await self.restore_ai_channels()

    # ---- AI 통역 채널 ----
    def ai_token_provider(self, channel_id: int):
        """AI 워커가 매 (재)접속 시 호출하는 fresh publish 토큰 프로바이더(HIGH1).

        채널 슬롯 기준으로 lease 를 재획득(force)하고 새 identity 로 publish 토큰을 발급한다.
        슈퍼바이저가 재시작할 때마다 새 JWT·lease 를 받으므로 '최초 1시간 토큰 재사용 →
        1시간 뒤 영구 재접속 거절' 결함이 사라진다. 워커가 join 하기 전에 lease.identity 가
        먼저 갱신되므로 webhook 강제(identity 일치)도 통과한다. 채널이 닫혔으면 예외를 던져
        슈퍼바이저 루프가 (stop 신호와 함께) 자연 종료하게 한다.
        """

        async def provider() -> str:
            ch = self.db.get_channel(channel_id)
            if ch is None or ch.state != "open" or ch.source != "ai":
                raise RuntimeError(f"ai channel {channel_id} not open")
            nonce = new_nonce()
            identity = self.db.force_acquire_lease(
                channel_id, ch.epoch, self.generation, nonce, PUBLISH_TTL_SECONDS
            )
            return issue_publish_token(
                self.settings.livekit_api_key,
                self.settings.livekit_api_secret,
                self.generation,
                identity,
                can_publish_data=True,  # 워커는 자막을 data 패킷으로 발행한다.
            )

        return provider

    async def restore_ai_channels(self) -> list[int]:
        """재시작 후에도 열려 있는 AI 통역 채널의 워커를 다시 스폰한다.

        복원 실패(토큰 발급 불가 등)한 채널은 슬롯을 영구 점유하지 않도록 닫는다.
        워커의 실제 접속 성공은 기다리지 않는다 — 접속은 슈퍼바이저가 백오프 재시도로
        수렴시키며, 여기서 대기하면 LiveKit·OpenAI 지연이 서버 기동을 막기 때문이다.
        """
        restored: list[int] = []
        for ch in self.db.list_open_ai_channels():
            cid = ch.channel_id
            try:
                provider = self.ai_token_provider(cid)
                params = AiWorkerParams(
                    channel_id=cid,
                    target_language=ch.target_language or "",
                    rtc_url=self.settings.livekit_rtc_url,
                    token=await provider(),  # 최초 접속 토큰(+lease 재획득)
                    room=self.room,
                    publish_track=f"ch-{cid:02d}",
                    subscribe_track=f"ch-{(ch.source_channel or 0):02d}",
                )
                self.ai_channels.start(params, token_provider=provider)
                restored.append(cid)
            except Exception:  # noqa: BLE001 — 한 채널 실패가 나머지 복원을 막지 않게
                log.exception("ai_channel_restore_failed ch=%s", cid)
                self.db.close_channel(cid)
        if restored:
            log.info("restored_ai_channels_on_bootstrap channels=%s", restored)
        return restored
