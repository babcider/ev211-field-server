# 모드 C AI 통역 라이브 E2E — Floor(ch-00) 원음 송출 → 번역 워커 → ch-NN 청취까지 실제 파이프라인 검증
"""라이브 LiveKit + 실 OpenAI 키로 AI 통역 전 구간을 검증하는 하니스.

단위 테스트(mock)와 test_openai_e2e.py(OpenAI 단독)가 덮지 못하는 구간 —
LiveKit 구독 → 리샘플 → OpenAI append → republish → 청취자 수신 — 을 실제로 통과시킨다.

사용:
    .venv/bin/python scripts/ai_live_e2e.py \
        --base http://127.0.0.1:8880 --send-password 0211 \
        --wav /path/to/floor_en_48k.wav --target-language ko

전제: docker compose 로 livekit·field-api 가 떠 있고 field-api 에 OPENAI_API_KEY 가 주입돼 있을 것.
종료 시 개설한 AI 채널·Floor 채널을 정리한다(실패해도 로그만 남기고 진행).
"""
from __future__ import annotations

import argparse
import array
import asyncio
import json
import math
import time
import urllib.error
import urllib.request
import wave

from livekit import rtc

FRAME_MS = 20
CAPTION_TOPIC = "captions"  # app.config.AI_CAPTION_TOPIC 과 동일해야 한다.
FLOOR_RATE = 48_000
LISTEN_RATE = 24_000  # 워커 출력(OpenAI 24kHz)과 동일하게 받아 리샘플 오차를 배제한다.
SILENCE_RMS = 200.0  # 이 값을 넘는 프레임을 "소리 있음"으로 본다(PCM16 기준).


# ---- HTTP ----
def call(base: str, path: str, method: str = "GET", body: dict | None = None,
         password: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if password:
        req.add_header("Authorization", f"Bearer {password}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


# ---- 오디오 ----
def rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def load_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("입력 WAV 는 mono 16-bit 여야 합니다.")
        return w.readframes(w.getnframes()), w.getframerate()


class FloorPublisher:
    """Floor(ch-00) 로 무음을 상시 송출하다가 신호를 받으면 원음 WAV 를 흘려보낸다."""

    def __init__(self, url: str, token: str, track_name: str, pcm: bytes, rate: int) -> None:
        self._url, self._token, self._track_name = url, token, track_name
        self._pcm, self._rate = pcm, rate
        self._room = rtc.Room()
        self._source = rtc.AudioSource(rate, 1)
        self._speak = asyncio.Event()
        self._done = asyncio.Event()
        self.speak_started_at: float | None = None

    async def connect(self) -> None:
        await self._room.connect(self._url, self._token, rtc.RoomOptions(auto_subscribe=False))
        track = rtc.LocalAudioTrack.create_audio_track(self._track_name, self._source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await self._room.local_participant.publish_track(track, opts)

    def start_speaking(self) -> None:
        self.speak_started_at = time.time()
        self._speak.set()

    async def wait_done(self) -> None:
        await self._done.wait()

    async def pump(self) -> None:
        """AudioSource 큐가 자체적으로 실시간 페이싱을 하므로 그대로 밀어 넣는다."""
        chunk = int(self._rate * FRAME_MS / 1000) * 2  # bytes / 20ms
        silence = bytes(chunk)
        # 발화 시작 신호 전까지 무음(워커·청취자가 붙을 시간).
        while not self._speak.is_set():
            await self._capture(silence)
        for off in range(0, len(self._pcm) - chunk, chunk):
            await self._capture(self._pcm[off:off + chunk])
        self._done.set()
        # 발화 후에도 무음을 계속 흘려 세션이 끊기지 않게 한다.
        while True:
            await self._capture(silence)

    async def _capture(self, pcm: bytes) -> None:
        samples = len(pcm) // 2
        await self._source.capture_frame(rtc.AudioFrame(pcm, self._rate, 1, samples))

    async def aclose(self) -> None:
        await self._room.disconnect()


class TranslationListener:
    """번역 채널(ch-NN)을 구독해 PCM 을 모으고 첫 소리 도달 시각을 기록한다."""

    def __init__(self, url: str, token: str, track_name: str) -> None:
        self._url, self._token, self._track_name = url, token, track_name
        self._room = rtc.Room()
        self._tasks: list[asyncio.Task] = []
        self.pcm = bytearray()
        self.frames = 0
        self.first_audio_at: float | None = None
        self.peak_rms = 0.0
        self.captions: list[dict] = []

    async def connect(self) -> None:
        @self._room.on("track_subscribed")
        def _on_sub(track, publication, _participant) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO or publication.name != self._track_name:
                return
            stream = rtc.AudioStream(track, sample_rate=LISTEN_RATE, num_channels=1,
                                     frame_size_ms=FRAME_MS)
            self._tasks.append(asyncio.create_task(self._drain(stream)))

        @self._room.on("data_received")
        def _on_data(packet) -> None:
            # 워커가 발행하는 자막 data 패킷(topic=captions)만 모은다.
            if getattr(packet, "topic", None) != CAPTION_TOPIC:
                return
            try:
                self.captions.append(json.loads(bytes(packet.data).decode()))
            except (ValueError, UnicodeDecodeError):
                pass

        await self._room.connect(self._url, self._token, rtc.RoomOptions(auto_subscribe=True))

    async def _drain(self, stream: "rtc.AudioStream") -> None:
        async for event in stream:
            pcm = event.frame.data.tobytes()
            self.pcm.extend(pcm)
            self.frames += 1
            level = rms(pcm)
            self.peak_rms = max(self.peak_rms, level)
            if level > SILENCE_RMS and self.first_audio_at is None:
                self.first_audio_at = time.time()

    def save(self, path: str) -> None:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(LISTEN_RATE)
            w.writeframes(bytes(self.pcm))

    async def aclose(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._room.disconnect()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8880")
    ap.add_argument("--send-password", required=True)
    ap.add_argument("--wav", required=True, help="원음 WAV(mono 16-bit)")
    ap.add_argument("--target-language", default="ko")
    ap.add_argument("--tail-seconds", type=float, default=20.0,
                    help="원음 송출이 끝난 뒤 번역 오디오를 더 기다릴 시간")
    ap.add_argument("--out", default="/tmp/ai_translation_out.wav")
    args = ap.parse_args()

    pcm, rate = load_wav(args.wav)
    if rate != FLOOR_RATE:
        print(f"! 입력 WAV 가 {rate}Hz 입니다 — Floor 는 {FLOOR_RATE}Hz 를 권장합니다(그대로 진행).")
    print(f"원음 {len(pcm) / (rate * 2):.1f}초 · 목표 언어 {args.target_language}")

    # 1) Floor(ch-00) 채널 확보 + 송신 토큰.
    status, _ = call(args.base, "/channels", "POST",
                     {"channel_id": 0, "language": "en", "label": "Floor(E2E)"},
                     args.send_password)
    print(f"[1] Floor 채널: {status}")
    status, pub = call(args.base, "/publish-tokens", "POST", {"channel_id": 0},
                       args.send_password)
    if status != 200:
        print(f"송신 토큰 실패 {status}: {pub}")
        return 1
    print(f"[2] 송신 토큰: {pub['identity']} → {pub['track_name']} @ {pub['url']}")

    publisher = FloorPublisher(pub["url"], pub["token"], pub["track_name"], pcm, rate)
    await publisher.connect()
    pump = asyncio.create_task(publisher.pump())
    await asyncio.sleep(1.0)  # 트랙 발행이 SFU 에 반영될 시간.

    ai_channel_id = None
    listener = None
    try:
        # 2) AI 통역 채널 개설(워커가 ch-00 구독 → 번역 → ch-NN publish).
        t_create = time.time()
        status, ai = call(args.base, "/ai-channels", "POST",
                          {"target_language": args.target_language, "source_channel": 0},
                          args.send_password)
        if status != 201:
            print(f"[3] AI 채널 개설 실패 {status}: {ai}")
            return 1
        ai_channel_id = ai["channel_id"]
        print(f"[3] AI 채널 {ai_channel_id}({ai['track_name']}) 개설 "
              f"— 워커 준비 {time.time() - t_create:.1f}초")

        # 3) 청취자 접속(번역 채널 구독).
        status, sub = call(args.base, f"/channels/{ai_channel_id}/subscribe-tokens", "POST")
        if status != 200:
            print(f"[4] 수신 토큰 실패 {status}: {sub}")
            return 1
        listener = TranslationListener(sub["url"], sub["token"], sub["track_name"])
        await listener.connect()
        await asyncio.sleep(1.5)
        print(f"[4] 청취자 접속 · {sub['track_name']} 구독 대기")

        # 4) 원음 송출 시작 → 끝날 때까지 + tail 대기.
        publisher.start_speaking()
        print("[5] 원음 송출 시작")
        await publisher.wait_done()
        print(f"[6] 원음 송출 완료 — 번역 오디오 {args.tail_seconds:.0f}초 더 수집")
        await asyncio.sleep(args.tail_seconds)

        # 5) 워커 헬스.
        status, health = call(args.base, f"/ai-channels/{ai_channel_id}/status", "GET",
                              password=args.send_password)
        worker = health.get("worker") or {}
        print(f"[7] 워커: running={worker.get('running')} seq={worker.get('seq')} "
              f"caption_seq={worker.get('caption_seq')} renewals={worker.get('renewals')} "
              f"restarts={worker.get('restart_count')} last_error={worker.get('last_error')}")

        # 6) 판정.
        seconds = len(listener.pcm) / (LISTEN_RATE * 2)
        latency = (listener.first_audio_at - publisher.speak_started_at
                   if listener.first_audio_at and publisher.speak_started_at else None)
        listener.save(args.out)
        target_text = "".join(c["delta"] for c in listener.captions if c.get("kind") == "target")
        source_text = "".join(c["delta"] for c in listener.captions if c.get("kind") == "source")
        print("─" * 60)
        print(f"수신 프레임 {listener.frames} · {seconds:.1f}초 · peak RMS {listener.peak_rms:.0f}")
        print(f"첫 번역 소리까지 {latency:.1f}초" if latency else "번역 소리 없음(무음)")
        print(f"자막 패킷 {len(listener.captions)}건")
        print(f"  원문: {source_text[:120]}")
        print(f"  번역: {target_text[:120]}")
        print(f"저장: {args.out}")
        audio_ok = listener.peak_rms > SILENCE_RMS and worker.get("seq", 0) > 0
        caption_ok = bool(target_text)
        print(f"판정: 오디오 {'PASS' if audio_ok else 'FAIL'} · "
              f"자막 {'PASS' if caption_ok else 'FAIL'}")
        return 0 if (audio_ok and caption_ok) else 1
    finally:
        pump.cancel()
        if listener is not None:
            await listener.aclose()
        await publisher.aclose()
        if ai_channel_id is not None:
            call(args.base, f"/ai-channels/{ai_channel_id}", "DELETE",
                 password=args.send_password)
        call(args.base, "/channels/0", "DELETE", password=args.send_password)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
