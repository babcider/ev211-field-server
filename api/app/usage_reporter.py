# 종료된 사용량 세션을 ev211.com 으로 push 하는 보고기 (라이브 API 계약 §4)
from __future__ import annotations

import datetime as _dt
import logging

import aiohttp

from .db import UsageSessionRow

log = logging.getLogger("field.usage")

PUSH_TIMEOUT_SECONDS = 10
BATCH_SIZE = 100
MAX_ATTEMPTS = 10  # 이 횟수를 넘겨 실패하면 보고를 포기한다(무한 재시도 방지).


def _iso(ts: float) -> str:
    """epoch 초를 계약이 요구하는 UTC ISO8601 문자열로 바꾼다."""
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def session_to_event(row: UsageSessionRow) -> dict:
    """세션 행을 계약 §4 의 이벤트 오브젝트로 변환한다."""
    ended_at = row.ended_at if row.ended_at is not None else row.started_at
    event = {
        "join_code": row.join_code,
        "kind": row.kind,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(ended_at),
        "seconds": max(0, int(round(ended_at - row.started_at))),
        "participant_hash": row.subject_hash,
    }
    if row.language:
        event["language"] = row.language
    return event


class UsageReporter:
    """종료된 세션을 배치로 ev211.com `POST /api/field/usage` 에 보고한다.

    - 성공(201)한 배치만 pushed_at 을 남긴다 — 실패는 다음 스윕에서 다시 보낸다.
    - 재시도 상한(MAX_ATTEMPTS)을 넘긴 세션은 보고를 포기하고 pushed_at 을 찍는다
      (로컬 원장 signal_events 는 그대로 남으므로 사후 추적은 가능하다, 계약 §7).
    """

    def __init__(self, api_base: str, secret: str) -> None:
        self._api_base = api_base.rstrip("/")
        self._secret = secret

    @property
    def enabled(self) -> bool:
        return bool(self._api_base and self._secret)

    async def flush(self, db) -> tuple[int, int]:
        """미보고 세션을 한 배치 보고한다. 반환: (보고 성공 건수, 포기 건수)."""
        if not self.enabled:
            return 0, 0
        rows = db.pending_usage_sessions(limit=BATCH_SIZE)
        if not rows:
            return 0, 0

        # 재시도 상한을 넘긴 세션은 이번 배치에서 떼어내 포기 처리한다.
        giveup = [r for r in rows if r.attempts >= MAX_ATTEMPTS]
        sendable = [r for r in rows if r.attempts < MAX_ATTEMPTS]
        if giveup:
            db.mark_usage_sessions_pushed([r.session_id for r in giveup])
            log.warning("usage_push_giveup count=%s", len(giveup))
        if not sendable:
            return 0, len(giveup)

        events = [session_to_event(r) for r in sendable]
        ids = [r.session_id for r in sendable]
        if await self._post(events):
            db.mark_usage_sessions_pushed(ids)
            log.info("usage_push_ok count=%s", len(ids))
            return len(ids), len(giveup)

        db.bump_usage_session_attempts(ids)
        return 0, len(giveup)

    async def _post(self, events: list[dict]) -> bool:
        """계약 §4 호출. 테스트에서 이 메서드를 대체한다."""
        url = f"{self._api_base}/api/field/usage"
        try:
            timeout = aiohttp.ClientTimeout(total=PUSH_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json={"events": events},
                    headers={"X-Field-Secret": self._secret},
                ) as resp:
                    if resp.status != 201:
                        log.warning("usage_push_failed status=%s", resp.status)
                        return False
                    return True
        except Exception:  # noqa: BLE001 — 네트워크 오류는 다음 스윕에 재시도
            log.warning("usage_push_error url=%s", url)
            return False
