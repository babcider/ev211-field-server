# 라이브별 송신 비번 콜백 검증 — ev211.com verify_send 위임 + 5분 긍정 캐시 (라이브 API 계약 §3·§7)
from __future__ import annotations

import asyncio
import time

import aiohttp

CACHE_TTL_SECONDS = 300  # 긍정 결과만 캐시(부정 캐시 금지 — 발급 직후 입장 실패 방지)
VERIFY_TIMEOUT_SECONDS = 5
CACHE_MAX_ENTRIES = 1024  # 초과 시 만료 항목 정리(무한 성장 방지)


class LiveSendVerifier:
    """복합 Bearer(`<join_code>:<send_password>`)를 ev211.com 콜백으로 검증한다.

    - 검증 성공은 5분 캐시(콜백 부담·순단 흡수), 실패·오류는 캐시하지 않는다.
    - 네트워크 오류·비200 응답은 전부 False(fail-closed — 과금 누수 방지, 계약 §7).
    """

    def __init__(self, api_base: str, secret: str) -> None:
        self._api_base = api_base.rstrip("/")
        self._secret = secret
        self._cache: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def split_bearer(token: str | None) -> tuple[str, str] | None:
        """복합 Bearer 를 (join_code, send_password) 로 분리한다. 형식 불일치는 None."""
        if not token or ":" not in token:
            return None
        code, password = token.split(":", 1)
        code = code.strip().upper()
        password = password.strip()
        if not code or not password:
            return None
        return code, password

    async def verify(self, token: str | None) -> bool:
        pair = self.split_bearer(token)
        if pair is None:
            return False
        now = time.monotonic()
        expires = self._cache.get(pair)
        if expires is not None and expires > now:
            return True
        ok = await self._call_verify(pair[0], pair[1])
        if ok:
            async with self._lock:
                if len(self._cache) >= CACHE_MAX_ENTRIES:
                    self._cache = {k: v for k, v in self._cache.items() if v > now}
                self._cache[pair] = time.monotonic() + CACHE_TTL_SECONDS
        return ok

    async def _call_verify(self, join_code: str, send_password: str) -> bool:
        """ev211.com `POST /api/field/verify_send` 호출. 테스트에서 이 메서드를 대체한다."""
        url = f"{self._api_base}/api/field/verify_send"
        try:
            timeout = aiohttp.ClientTimeout(total=VERIFY_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json={"join_code": join_code, "send_password": send_password},
                    headers={"X-Field-Secret": self._secret},
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    return bool(data.get("ok"))
        except Exception:
            return False
