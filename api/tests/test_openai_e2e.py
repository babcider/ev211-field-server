# gpt-realtime-translate 실 프로토콜 계약 E2E — 기본 skip, RUN_OPENAI_E2E=1 + OPENAI_API_KEY 일 때만 실행.
# 워커가 보내는 client→server 이벤트 프리픽스(session.*)와 세션 스키마가 실 API 와 맞는지 잠근다.
from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_OPENAI_E2E") or not os.getenv("OPENAI_API_KEY"),
    reason="실 OpenAI 키·네트워크·비용 필요 — RUN_OPENAI_E2E=1 + OPENAI_API_KEY 로만 실행",
)

from app.config import (  # noqa: E402
    AI_INPUT_SAMPLE_RATE,
    AI_TRANSLATE_URL,
)


async def _run_contract() -> dict:
    import websockets

    key = os.environ["OPENAI_API_KEY"]
    url = AI_TRANSLATE_URL  # 이미 ?model=gpt-realtime-translate 포함
    hdr = {"Authorization": f"Bearer {key}"}
    try:
        ws = await websockets.connect(url, additional_headers=hdr)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=hdr)

    seen: list[str] = []
    errors: list[dict] = []
    session_type = None
    try:
        # 워커와 동일한 session.update(영어 입력 → 한국어 출력).
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"audio": {
                "input": {"transcription": {"model": "gpt-realtime-whisper"},
                          "noise_reduction": {"type": "near_field"}},
                "output": {"language": "ko"}}},
        }))
        # 워커와 동일한 프리픽스로 20ms 무음 append — 프리픽스/스키마가 틀리면 error 로 거부됨.
        silence = base64.b64encode(b"\x00\x00" * (AI_INPUT_SAMPLE_RATE // 50)).decode()
        await ws.send(json.dumps({"type": "session.input_audio_buffer.append", "audio": silence}))

        async def recv():
            while True:
                ev = json.loads(await ws.recv())
                t = ev.get("type", "?")
                seen.append(t)
                if t == "session.created":
                    nonlocal session_type
                    session_type = ev.get("session", {}).get("type")
                if t == "error":
                    errors.append(ev)

        try:
            await asyncio.wait_for(recv(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
    finally:
        await ws.close()
    return {"seen": seen, "errors": errors, "session_type": session_type}


def test_realtime_translate_client_event_contract():
    r = asyncio.run(_run_contract())
    # 세션이 생성·구성되고 type=translation.
    assert "session.created" in r["seen"], r["seen"]
    assert "session.updated" in r["seen"], r["seen"]
    assert r["session_type"] == "translation", r["session_type"]
    # 워커가 보내는 session.update / session.input_audio_buffer.append 가 거부되지 않는다.
    assert r["errors"] == [], r["errors"]
