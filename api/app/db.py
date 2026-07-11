# SQLite(WAL) 권위 원장 — 채널·lease·세대·처리한 웹훅 id·listener 발급/heartbeat 관리
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from .config import ISSUED_LISTENER_TTL_SECONDS


def _now() -> float:
    return time.time()


@dataclass
class ChannelRow:
    channel_id: int
    track_name: str
    language: str
    label: str
    state: str  # open | closed
    epoch: int
    created_at: float


@dataclass
class LeaseRow:
    channel_id: int
    identity: str
    epoch: int
    generation: int
    nonce: str
    expires_at: float
    joined_at: float | None  # participant_joined 웹훅 수신 시각(없으면 미접속)


@dataclass
class IntercomChannelRow:
    channel_id: int
    name: str
    password_hash: str | None  # 채널 입장 비밀번호 해시(선택). None 이면 자유 입장.
    created_at: float

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None


@dataclass
class SignalEventRow:
    event_id: int
    occurred_at: float
    direction: str
    event_type: str
    scope: str
    channel_id: int | None
    generation: int | None
    room: str | None
    track_name: str | None
    subject_hash: str | None
    client_ip: str | None
    source_event_id: str | None


class Database:
    """단일 SQLite 연결을 WAL 모드로 감싸는 원장. 프로세스 내 락으로 원자성을 보장한다.

    FastAPI 는 기본적으로 스레드풀에서 라우트를 실행할 수 있으므로 check_same_thread=False
    로 열되, 쓰기 경합은 SQLite 트랜잭션(BEGIN IMMEDIATE)으로 직렬화한다.
    """

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        # 단일 sqlite 연결을 FastAPI 스레드풀·이벤트루프에서 공유하므로 모든 접근을
        # 재진입 락으로 직렬화한다(읽기·쓰기 경합·부분 쓰기 노출 방지). sqlite 호출은
        # CPU 바운드로 짧아 동기 락으로 충분하며, _Tx 안에서 다시 잡을 수 있게 RLock 을 쓴다.
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """락으로 보호되는 읽기 헬퍼."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS generation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                value INTEGER NOT NULL,
                password_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                track_name TEXT NOT NULL,
                language TEXT NOT NULL,
                label TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                epoch INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leases (
                channel_id INTEGER PRIMARY KEY,
                identity TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                nonce TEXT NOT NULL,
                expires_at REAL NOT NULL,
                joined_at REAL
            );

            CREATE TABLE IF NOT EXISTS processed_webhooks (
                event_id TEXT PRIMARY KEY,
                processed_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issued_listeners (
                listener_id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                issued_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heartbeats (
                listener_id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscribe_token_counts (
                channel_id INTEGER PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- 무전기(인터컴) 채널 메타데이터. 룸은 intercom-g<gen>-c<id> 로 매핑되며,
            -- 세대 회전·재시작 시 룸과 함께 전부 폐기된다(clear_intercom_channels).
            CREATE TABLE IF NOT EXISTS intercom_channels (
                channel_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT,
                created_at REAL NOT NULL
            );

            -- 사용자 송수신 세션 감사 로그. 비밀번호·JWT·음성은 저장하지 않고
            -- participant 식별자는 애플리케이션에서 해시한 값만 저장한다.
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at REAL NOT NULL,
                direction TEXT NOT NULL,
                event_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                channel_id INTEGER,
                generation INTEGER,
                room TEXT,
                track_name TEXT,
                subject_hash TEXT,
                client_ip TEXT,
                source_event_id TEXT UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_signal_events_occurred_at
            ON signal_events(occurred_at);
            """
        )
        c.commit()

    # ---- 송수신 신호 이벤트 ----
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
        subject_hash: str | None = None,
        client_ip: str | None = None,
        source_event_id: str | None = None,
        occurred_at: float | None = None,
    ) -> bool:
        """신호 이벤트를 저장한다. 같은 LiveKit event id 재전송은 한 번만 저장한다."""
        try:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO signal_events "
                    "(occurred_at, direction, event_type, scope, channel_id, generation, "
                    "room, track_name, subject_hash, client_ip, source_event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now() if occurred_at is None else occurred_at,
                        direction,
                        event_type,
                        scope,
                        channel_id,
                        generation,
                        room,
                        track_name,
                        subject_hash,
                        client_ip,
                        source_event_id,
                    ),
                )
        except sqlite3.IntegrityError:
            if source_event_id is not None:
                return False
            raise
        return True

    def list_signal_events(self, limit: int = 200) -> list[SignalEventRow]:
        rows = self._query(
            "SELECT * FROM signal_events ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (max(1, min(limit, 10_000)),),
        )
        return [
            SignalEventRow(
                event_id=int(r["id"]),
                occurred_at=float(r["occurred_at"]),
                direction=str(r["direction"]),
                event_type=str(r["event_type"]),
                scope=str(r["scope"]),
                channel_id=None if r["channel_id"] is None else int(r["channel_id"]),
                generation=None if r["generation"] is None else int(r["generation"]),
                room=None if r["room"] is None else str(r["room"]),
                track_name=None if r["track_name"] is None else str(r["track_name"]),
                subject_hash=None if r["subject_hash"] is None else str(r["subject_hash"]),
                client_ip=None if r["client_ip"] is None else str(r["client_ip"]),
                source_event_id=(
                    None if r["source_event_id"] is None else str(r["source_event_id"])
                ),
            )
            for r in rows
        ]

    def purge_signal_events(self, retention_seconds: int, now: float | None = None) -> int:
        """보관 기간을 초과한 신호 이벤트를 삭제하고 삭제 행 수를 반환한다."""
        cutoff = (_now() if now is None else now) - retention_seconds
        with self._tx():
            cursor = self._conn.execute(
                "DELETE FROM signal_events WHERE occurred_at < ?", (cutoff,)
            )
        return max(0, cursor.rowcount)

    # ---- 런타임 설정(비밀번호 오버라이드 등) ----
    def get_setting(self, key: str) -> str | None:
        row = self._query_one("SELECT value FROM settings WHERE key=?", (key,))
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _set_password_and_generation(
        self, key: str, password: str, gen: int, password_hash: str
    ) -> None:
        """비밀번호와 세대 해시를 단일 트랜잭션으로 저장한다(중간 실패 시 둘 다 롤백).

        따로 저장하면 중간 실패·프로세스 종료 시 비밀번호와 해시가 불일치해 다음
        재시작에서 의도치 않은 세대 회전이 발생한다(12차 #5).
        """
        with self._tx():
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, password),
            )
            self._conn.execute(
                "INSERT INTO generation (id, value, password_hash) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value, password_hash=excluded.password_hash",
                (gen, password_hash),
            )

    def set_admin_password_and_generation(self, password: str, gen: int, password_hash: str) -> None:
        self._set_password_and_generation("admin_password", password, gen, password_hash)

    def rotate_send_password(self, password: str, gen: int, password_hash: str) -> None:
        """송신자 비밀번호 회전을 단일 트랜잭션으로 커밋한다(13차 #4·22차).

        비밀번호·세대 해시 갱신, **전체 lease 폐기**, **인터컴 채널 메타데이터 폐기**를
        한 트랜잭션에 묶는다 — 회전 커밋 후 별도 삭제 실패로 '비밀번호는 바뀌었는데
        구세대 채널 비밀번호 메타데이터가 새 세대에서 재사용되는' 상태를 없앤다(22차).
        """
        with self._tx():
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('send_password', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (password,),
            )
            self._conn.execute(
                "INSERT INTO generation (id, value, password_hash) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value, password_hash=excluded.password_hash",
                (gen, password_hash),
            )
            self._conn.execute("DELETE FROM leases")
            self._conn.execute("DELETE FROM intercom_channels")

    # ---- 세대(generation) ----
    def get_generation(self) -> tuple[int, str] | None:
        row = self._query_one("SELECT value, password_hash FROM generation WHERE id=1")
        if row is None:
            return None
        return int(row["value"]), row["password_hash"]

    def set_generation(self, value: int, password_hash: str) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO generation (id, value, password_hash) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value, password_hash=excluded.password_hash",
                (value, password_hash),
            )

    # ---- 채널 ----
    def list_channels(self) -> list[ChannelRow]:
        rows = self._query("SELECT * FROM channels ORDER BY channel_id")
        return [self._to_channel(r) for r in rows]

    def get_channel(self, channel_id: int) -> ChannelRow | None:
        r = self._query_one("SELECT * FROM channels WHERE channel_id=?", (channel_id,))
        return self._to_channel(r) if r else None

    @staticmethod
    def _to_channel(r: sqlite3.Row) -> ChannelRow:
        return ChannelRow(
            channel_id=r["channel_id"],
            track_name=r["track_name"],
            language=r["language"],
            label=r["label"],
            state=r["state"],
            epoch=r["epoch"],
            created_at=r["created_at"],
        )

    def count_open_general_channels(self) -> int:
        r = self._query_one(
            "SELECT COUNT(*) AS n FROM channels WHERE state='open' AND channel_id != 0"
        )
        return int(r["n"])

    def lowest_free_general_slot(self, max_channels: int) -> int | None:
        used = {
            r["channel_id"]
            for r in self._query("SELECT channel_id FROM channels WHERE state='open'")
        }
        for cid in range(1, max_channels + 1):
            if cid not in used:
                return cid
        return None

    def create_channel(self, channel_id: int, language: str, label: str) -> ChannelRow:
        """채널 슬롯을 개설한다(원자적). 이미 open 이면 IntegrityError(호출부에서 409 처리)."""
        track = f"ch-{channel_id:02d}"
        now = _now()
        with self._tx():
            existing = self._conn.execute(
                "SELECT state FROM channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if existing is not None and existing["state"] == "open":
                raise ChannelExists(channel_id)
            # closed 였던 채널은 다시 open 으로(epoch 유지), 없으면 새로 삽입.
            self._conn.execute(
                "INSERT INTO channels (channel_id, track_name, language, label, state, epoch, created_at) "
                "VALUES (?, ?, ?, ?, 'open', 1, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "language=excluded.language, label=excluded.label, state='open', created_at=excluded.created_at",
                (channel_id, track, language, label, now),
            )
        return self.get_channel(channel_id)  # type: ignore[return-value]

    def close_channel(self, channel_id: int) -> None:
        with self._tx():
            self._conn.execute("UPDATE channels SET state='closed' WHERE channel_id=?", (channel_id,))
            self._conn.execute("DELETE FROM leases WHERE channel_id=?", (channel_id,))

    # ---- lease(원자적 점유) ----
    def get_lease(self, channel_id: int) -> LeaseRow | None:
        r = self._query_one("SELECT * FROM leases WHERE channel_id=?", (channel_id,))
        if r is None:
            return None
        return LeaseRow(
            channel_id=r["channel_id"],
            identity=r["identity"],
            epoch=r["epoch"],
            generation=r["generation"],
            nonce=r["nonce"],
            expires_at=r["expires_at"],
            joined_at=r["joined_at"],
        )

    def acquire_lease(
        self,
        channel_id: int,
        epoch: int,
        generation: int,
        nonce: str,
        ttl_seconds: int,
        grace_seconds: int = 0,
    ) -> tuple[bool, str]:
        """채널 lease 를 원자적으로 획득한다.

        반환: (성공여부, identity). 살아 있는 lease 가 있으면 (False, "").

        "살아 있음" 판정(codex 7차 결함 #6):
        - **joined lease**(participant_joined 웹훅 수신됨): TTL(expires_at)이 미래면 살아 있음.
          활성 송신 중이므로 만료 전 재발급을 거부한다(409).
        - **미접속 lease**(joined_at NULL): 발급 후 `grace_seconds` 안에는 접속 대기 중으로
          간주해 재발급을 거부하되, grace 를 지나도록 접속하지 않았으면 발급 실패로 보고
          **자동 해제 가능**으로 판정한다(발급 후 접속 실패 시 1시간 409 방지).
        identity 는 speaker-ch-NN-e<epoch>-g<gen>-n<nonce> 규약으로 생성한다.
        """
        now = _now()
        identity = f"speaker-ch-{channel_id:02d}-e{epoch}-g{generation}-n{nonce}"
        with self._tx():
            existing = self._conn.execute(
                "SELECT expires_at, joined_at FROM leases WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if existing is not None and self._lease_alive(existing, now, ttl_seconds, grace_seconds):
                return False, ""
            self._conn.execute(
                "INSERT INTO leases (channel_id, identity, epoch, generation, nonce, expires_at, joined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "identity=excluded.identity, epoch=excluded.epoch, generation=excluded.generation, "
                "nonce=excluded.nonce, expires_at=excluded.expires_at, joined_at=NULL",
                (channel_id, identity, epoch, generation, nonce, now + ttl_seconds),
            )
        return True, identity

    @staticmethod
    def _lease_alive(row, now: float, ttl_seconds: int, grace_seconds: int) -> bool:
        """기존 lease 가 여전히 채널을 점유(재발급 거부)하는지 판정한다."""
        expires_at = row["expires_at"]
        joined_at = row["joined_at"]
        if joined_at is not None:
            # 접속 완료된 활성 송신 — TTL 남아 있으면 점유.
            return expires_at > now
        # 미접속 lease.
        if grace_seconds <= 0:
            # grace 미적용 — 기존 의미(TTL 동안 점유).
            return expires_at > now
        # grace 적용 — 발급 시각 + grace 안에서만 점유(접속 대기). 그 뒤엔 해제 가능.
        issued_at = expires_at - ttl_seconds
        return now < issued_at + grace_seconds

    def force_acquire_lease(
        self, channel_id: int, epoch: int, generation: int, nonce: str, ttl_seconds: int
    ) -> str:
        """takeover 전용 — 기존 lease 를 무조건 덮어써 새 lease 를 잡는다."""
        now = _now()
        identity = f"speaker-ch-{channel_id:02d}-e{epoch}-g{generation}-n{nonce}"
        with self._tx():
            self._conn.execute(
                "INSERT INTO leases (channel_id, identity, epoch, generation, nonce, expires_at, joined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "identity=excluded.identity, epoch=excluded.epoch, generation=excluded.generation, "
                "nonce=excluded.nonce, expires_at=excluded.expires_at, joined_at=NULL",
                (channel_id, identity, epoch, generation, nonce, now + ttl_seconds),
            )
        return identity

    def release_lease_if_identity(self, channel_id: int, identity: str) -> bool:
        """identity 전체 문자열이 현재 lease 와 정확히 일치할 때만 해제한다(stale left 방어)."""
        with self._tx():
            r = self._conn.execute(
                "SELECT identity FROM leases WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if r is None or r["identity"] != identity:
                return False
            self._conn.execute("DELETE FROM leases WHERE channel_id=?", (channel_id,))
        return True

    def release_lease(self, channel_id: int) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM leases WHERE channel_id=?", (channel_id,))

    def mark_lease_joined(self, channel_id: int, identity: str) -> None:
        """participant_joined 웹훅으로 접속 시각을 기록한다(현재 lease identity 일치 시)."""
        with self._tx():
            self._conn.execute(
                "UPDATE leases SET joined_at=? WHERE channel_id=? AND identity=?",
                (_now(), channel_id, identity),
            )

    def extend_lease_if_identity(self, channel_id: int, identity: str, ttl_seconds: int) -> bool:
        """현재 lease identity 가 일치하면 TTL 을 연장한다(활성 송신 lease 자동 연장, #6②)."""
        with self._tx():
            r = self._conn.execute(
                "SELECT identity FROM leases WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if r is None or r["identity"] != identity:
                return False
            self._conn.execute(
                "UPDATE leases SET expires_at=? WHERE channel_id=?",
                (_now() + ttl_seconds, channel_id),
            )
        return True

    def clear_all_leases(self) -> None:
        """세대 변경 시 구세대 lease 를 전부 정리한다(#8)."""
        with self._tx():
            self._conn.execute("DELETE FROM leases")

    def bump_channel_epoch(self, channel_id: int) -> int:
        with self._tx():
            self._conn.execute(
                "UPDATE channels SET epoch=epoch+1 WHERE channel_id=?", (channel_id,)
            )
            r = self._conn.execute(
                "SELECT epoch FROM channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return int(r["epoch"])

    # ---- 웹훅 멱등 ----
    def is_webhook_processed(self, event_id: str) -> bool:
        """이벤트 id 가 이미 처리(커밋)됐는지 읽기 전용으로 확인한다(#1 선-확인).

        처리 성공 후에만 mark_webhook_processed 로 커밋하므로, 이 값이 True 이면
        과거에 처리가 성공했다는 뜻이다. 처리 도중 실패한 이벤트는 커밋되지 않아
        False 로 남고 LiveKit 재전송 시 재처리된다.
        """
        r = self._query_one(
            "SELECT 1 FROM processed_webhooks WHERE event_id=? LIMIT 1", (event_id,)
        )
        return r is not None

    def mark_webhook_processed(self, event_id: str) -> bool:
        """처리 성공한 이벤트 id 를 원장에 커밋한다(#1). 이미 있으면 False(멱등)."""
        try:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO processed_webhooks (event_id, processed_at) VALUES (?, ?)",
                    (event_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    # ---- listener 발급/heartbeat ----
    def issue_listener(self, channel_id: int) -> str:
        listener_id = _uuid4()
        with self._tx():
            self._conn.execute(
                "INSERT INTO issued_listeners (listener_id, channel_id, issued_at) VALUES (?, ?, ?)",
                (listener_id, channel_id, _now()),
            )
            self._conn.execute(
                "INSERT INTO subscribe_token_counts (channel_id, count) VALUES (?, 1) "
                "ON CONFLICT(channel_id) DO UPDATE SET count=count+1",
                (channel_id,),
            )
        return listener_id

    def is_listener_issued(self, listener_id: str) -> bool:
        return self.issued_listener_channel(listener_id) is not None

    def issued_listener_channel(self, listener_id: str) -> int | None:
        """발급 원장에서 listener_id 의 채널을 반환한다(미발급·TTL 만료면 None).

        TTL(ISSUED_LISTENER_TTL_SECONDS) 경과분은 조회 시 lazy 정리하고, 만료된
        listener 는 미발급으로 취급한다(#10).
        """
        now = _now()
        r = self._query_one(
            "SELECT channel_id, issued_at FROM issued_listeners WHERE listener_id=?",
            (listener_id,),
        )
        if r is None:
            return None
        if r["issued_at"] + ISSUED_LISTENER_TTL_SECONDS <= now:
            return None
        return int(r["channel_id"])

    def record_heartbeat(self, listener_id: str, channel_id: int, ttl_seconds: int) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO heartbeats (listener_id, channel_id, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(listener_id) DO UPDATE SET channel_id=excluded.channel_id, expires_at=excluded.expires_at",
                (listener_id, channel_id, _now() + ttl_seconds),
            )

    def count_heartbeats_by_channel(self) -> dict[int, int]:
        now = _now()
        rows = self._query(
            "SELECT channel_id, COUNT(*) AS n FROM heartbeats WHERE expires_at > ? GROUP BY channel_id",
            (now,),
        )
        return {int(r["channel_id"]): int(r["n"]) for r in rows}

    def has_any_live_heartbeat(self) -> bool:
        r = self._query_one("SELECT 1 FROM heartbeats WHERE expires_at > ? LIMIT 1", (_now(),))
        return r is not None

    def token_approximation_by_channel(self) -> dict[int, int]:
        rows = self._query("SELECT channel_id, count FROM subscribe_token_counts")
        return {int(r["channel_id"]): int(r["count"]) for r in rows}

    def purge_expired_heartbeats(self) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM heartbeats WHERE expires_at <= ?", (_now(),))

    def purge_expired_issued_listeners(self) -> None:
        """TTL 경과한 발급 listener 원장을 정리하고, 그만큼 근사 카운트를 감산한다(#10).

        token_approximation 은 만료된 발급분을 계속 세지 않도록 채널별로 실제 살아 있는
        발급 수로 재동기화한다(음수 방지).
        """
        cutoff = _now() - ISSUED_LISTENER_TTL_SECONDS
        with self._tx():
            self._conn.execute("DELETE FROM issued_listeners WHERE issued_at <= ?", (cutoff,))
            # 근사 카운트를 살아 있는 발급 수로 재동기화.
            self._conn.execute("DELETE FROM subscribe_token_counts")
            self._conn.execute(
                "INSERT INTO subscribe_token_counts (channel_id, count) "
                "SELECT channel_id, COUNT(*) FROM issued_listeners GROUP BY channel_id"
            )

    # ---- 무전기(인터컴) 채널 ----
    def list_intercom_channels(self) -> list["IntercomChannelRow"]:
        rows = self._query("SELECT * FROM intercom_channels ORDER BY channel_id")
        return [self._to_intercom(r) for r in rows]

    def get_intercom_channel(self, channel_id: int) -> "IntercomChannelRow | None":
        r = self._query_one(
            "SELECT * FROM intercom_channels WHERE channel_id=?", (channel_id,)
        )
        return self._to_intercom(r) if r else None

    @staticmethod
    def _to_intercom(r: sqlite3.Row) -> "IntercomChannelRow":
        return IntercomChannelRow(
            channel_id=r["channel_id"],
            name=r["name"],
            password_hash=r["password_hash"],
            created_at=r["created_at"],
        )

    def lowest_free_intercom_slot(self, max_channels: int) -> int | None:
        used = {r["channel_id"] for r in self._query("SELECT channel_id FROM intercom_channels")}
        for cid in range(max_channels):
            if cid not in used:
                return cid
        return None

    def create_intercom_channel(
        self, channel_id: int, name: str, password_hash: str | None
    ) -> "IntercomChannelRow":
        """인터컴 채널을 개설한다(원자적). 이미 있으면 ChannelExists(호출부 409)."""
        now = _now()
        with self._tx():
            existing = self._conn.execute(
                "SELECT 1 FROM intercom_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if existing is not None:
                raise ChannelExists(channel_id)
            self._conn.execute(
                "INSERT INTO intercom_channels (channel_id, name, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, name, password_hash, now),
            )
        return IntercomChannelRow(channel_id, name, password_hash, now)

    def clear_intercom_channels(self) -> None:
        """모든 인터컴 채널 메타데이터를 삭제한다(세대 회전·재시작 시 룸과 함께 폐기)."""
        with self._tx():
            self._conn.execute("DELETE FROM intercom_channels")

    # ---- 트랜잭션 헬퍼 ----
    def _tx(self):
        return _Tx(self._conn, self._lock)


class _Tx:
    """BEGIN IMMEDIATE 로 쓰기 락을 즉시 잡아 채널/lease 경합을 직렬화한다.

    프로세스 내 RLock 도 함께 잡아 다른 스레드의 읽기/쓰기와 완전 직렬화한다.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self):
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._lock.release()
        return False


class ChannelExists(Exception):
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id
        super().__init__(f"channel {channel_id} already open")


def _uuid4() -> str:
    import uuid

    return str(uuid.uuid4())


def new_nonce() -> str:
    """lease 발급용 짧은 랜덤 nonce(영숫자 6자). identity 규약에 포함된다."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))
