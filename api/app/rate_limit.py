# 메모리 내 rate limit·실패 잠금·반복 위반 차단 목록(세대 변경·재시작 시 초기화)
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """키(IP 또는 listener_id)별 슬라이딩 윈도우 rate limit.

    per_minute 회를 60초 창으로 제한한다. 초과 시 (False, retry_after)."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self._per_minute:
                retry = int(60 - (now - q[0])) + 1
                return False, max(retry, 1)
            q.append(now)
            return True, 0


class FailureLock:
    """비밀번호 연속 실패 잠금. 임계 초과 시 lock_seconds 동안 잠근다."""

    def __init__(self, fail_limit: int, lock_seconds: int) -> None:
        self._fail_limit = fail_limit
        self._lock_seconds = lock_seconds
        self._fails: dict[str, int] = defaultdict(int)
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            until = self._locked_until.get(key, 0)
            if until > now:
                return True, int(until - now) + 1
            return False, 0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._fails[key] += 1
            if self._fails[key] >= self._fail_limit:
                self._locked_until[key] = now + self._lock_seconds
                self._fails[key] = 0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


class Blocklist:
    """반복 위반 identity 차단 목록(메모리). participant_joined 단계에서 즉시 제거 대상."""

    def __init__(self, strike_threshold: int = 3) -> None:
        self._threshold = strike_threshold
        self._strikes: dict[str, int] = defaultdict(int)
        self._blocked: set[str] = set()
        self._lock = threading.Lock()

    def strike(self, identity: str) -> None:
        with self._lock:
            self._strikes[identity] += 1
            if self._strikes[identity] >= self._threshold:
                self._blocked.add(identity)

    def is_blocked(self, identity: str) -> bool:
        with self._lock:
            return identity in self._blocked

    def clear(self) -> None:
        with self._lock:
            self._strikes.clear()
            self._blocked.clear()
