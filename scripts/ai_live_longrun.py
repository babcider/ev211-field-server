# 모드 C AI 통역 장시간(65분+) 실경과 하니스 — 55분 세션 갱신(make-before-break) 구간의 오디오 무중단 검증
"""`ai_live_e2e.py` 모듈을 재사용해 65분 이상 연속 송출하며 세션 교체 지점을 관측한다.

단위 테스트와 20~30초 E2E 가 덮지 못하는 구간 — **실제 55분을 넘겨 둘째 WS 를 워밍하고
원자 교체한 뒤 구세션을 8초 드레인하는 장면** — 을 실경과로 통과시킨다.

관측 방법
    - 청취자(ch-NN 구독)가 받는 20ms 프레임마다 (도착시각, RMS)를 기록한다.
      · 프레임 도착 간격이 벌어지면 = 트랙 자체가 멈춘 것(발행 중단·재발행).
      · 음성↔음성 사이 무음 구간 길이 = 귀에 들리는 공백.
    - 교체 시점 주변과 **같은 길이의 기준(baseline) 연속 발화 구간**을 비교한다.
      OpenAI 번역 출력은 원래도 문장 사이 무음이 있으므로, 절대값이 아니라
      기준 구간 분포와 비교해야 "교체 때문에 끊겼는지"를 판정할 수 있다.
    - 워커 카운터(`/ai-channels/{id}/status`)를 주기적으로 폴링해
      renewals 증가·restarts 0 유지·seq 진행·자막 seq 연속성을 확인한다.

송출 스케줄(비용 절감 + T1 게이팅 자극)
    기본은 [원음 클립 → 무음 N초] 반복이고, 아래 두 구간만 무음 없이 연속 발화한다.
      · 기준 구간: 경과 20:00~24:30
      · 교체 구간: 경과 53:30~58:00 (교체는 55:00 직후)
    구간 판정은 **경과 실시간** 기준이다 — 워커의 session_age 는 교체 순간 0 으로 되감기므로
    그 값으로 구간을 잡으면 교체 직후 연속 발화가 꺼져 정작 비교할 구간을 놓친다.

사용:
    .venv/bin/python scripts/ai_live_longrun.py \
        --base http://localhost:8880/api --send-password 0211 \
        --wav /tmp/floor_en_48k.wav --target-language ko

전제: docker compose 로 livekit·field-api 가 떠 있고 field-api 에 OPENAI_API_KEY 가 주입돼 있을 것.
주의: field-api 는 Caddy `/api` 프리픽스 뒤에 있다 — `--base` 에 `/api` 를 포함해야 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_live_e2e import (  # noqa: E402 — scripts/ 는 패키지가 아니라 경로 주입 후 임포트한다.
    CAPTION_TOPIC,
    FRAME_MS,
    LISTEN_RATE,
    SILENCE_RMS,
    call,
    load_wav,
    rms,
)
from livekit import rtc  # noqa: E402

# 프레임 도착 간격이 이 값을 넘으면 트랙 전달 자체가 멈춘 것으로 본다(정상은 20ms).
DELIVERY_GAP_SECONDS = 0.25
# 리포트에 남길 최소 무음 길이(이보다 짧은 틈은 문장 내 호흡이라 의미가 없다).
REPORT_GAP_MS = 200
# 교체 감지 정밀도를 위해 교체 예상 구간에서는 폴링 주기를 좁힌다(감지 불확실폭 = 이 값).
FAST_POLL_SECONDS = 2.0
SLOW_POLL_SECONDS = 10.0


class LongRunPublisher:
    """Floor(ch-00)로 [원음 클립 → 무음] 을 반복 송출한다. 연속 모드에서는 무음을 거의 없앤다."""

    def __init__(self, url: str, token: str, track_name: str, pcm: bytes, rate: int,
                 silence_seconds: float, continuous_gap_seconds: float) -> None:
        self._url, self._token, self._track_name = url, token, track_name
        self._pcm, self._rate = pcm, rate
        self._silence = silence_seconds
        self._continuous_gap = continuous_gap_seconds
        self._room = rtc.Room()
        self._source = rtc.AudioSource(rate, 1)
        self._chunk = int(rate * FRAME_MS / 1000) * 2  # bytes / 20ms
        self.continuous = False  # 폴러가 켜고 끈다(교체·기준 구간).
        self.speaking = False
        self.clips = 0
        self.speech_seconds = 0.0
        # (시작, 끝) 발화 구간 — 사후 분석에서 "말이 나가고 있던 시간대"를 판정하는 근거.
        self.speech_spans: list[tuple[float, float]] = []

    async def connect(self) -> None:
        await self._room.connect(self._url, self._token, rtc.RoomOptions(auto_subscribe=False))
        track = rtc.LocalAudioTrack.create_audio_track(self._track_name, self._source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await self._room.local_participant.publish_track(track, opts)

    async def pump(self, warmup_seconds: float) -> None:
        """AudioSource 큐가 실시간 페이싱을 하므로 그대로 밀어 넣는다(무한 루프, 취소로 종료)."""
        await self._silence_for(warmup_seconds)
        while True:
            await self._clip()
            gap = self._continuous_gap if self.continuous else self._silence
            await self._silence_for(gap)

    async def _clip(self) -> None:
        started = time.time()
        self.speaking = True
        for off in range(0, len(self._pcm) - self._chunk, self._chunk):
            await self._capture(self._pcm[off:off + self._chunk])
        self.speaking = False
        self.clips += 1
        ended = time.time()
        self.speech_seconds += ended - started
        self.speech_spans.append((started, ended))

    async def _silence_for(self, seconds: float) -> None:
        silence = bytes(self._chunk)
        frames = max(0, int(seconds * 1000 / FRAME_MS))
        for _ in range(frames):
            await self._capture(silence)

    async def _capture(self, pcm: bytes) -> None:
        await self._source.capture_frame(rtc.AudioFrame(pcm, self._rate, 1, len(pcm) // 2))

    async def aclose(self) -> None:
        await self._room.disconnect()


class LongRunListener:
    """번역 채널(ch-NN)을 구독해 프레임 도착시각·RMS 타임라인과 자막을 모은다.

    65분 전량 PCM 을 들고 있으면 200MB 가 되므로 오디오는 링버퍼로만 남기고,
    교체가 감지되면 그 앞뒤 구간만 WAV 로 떨군다(청취 증거용).
    """

    def __init__(self, url: str, token: str, track_name: str,
                 ring_seconds: float, capture_seconds: float) -> None:
        self._url, self._token, self._track_name = url, token, track_name
        self._room = rtc.Room()
        self._tasks: list[asyncio.Task] = []
        self.times: list[float] = []
        self.levels: list[float] = []
        self.captions: list[dict] = []
        self.first_audio_at: float | None = None
        self.peak_rms = 0.0
        self._ring: deque[bytes] = deque(maxlen=int(ring_seconds * 1000 / FRAME_MS))
        self._capture_frames = int(capture_seconds * 1000 / FRAME_MS)
        self._post: list[bytes] | None = None
        self.switch_clip: bytes | None = None

    def arm_switch_capture(self) -> None:
        """교체 감지 시각 기준 앞(링버퍼)+뒤(capture_seconds) 오디오를 저장하도록 무장한다."""
        if self._post is None and self.switch_clip is None:
            self._post = []

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
            if getattr(packet, "topic", None) != CAPTION_TOPIC:
                return
            try:
                payload = json.loads(bytes(packet.data).decode())
            except (ValueError, UnicodeDecodeError):
                return
            payload["_t"] = time.time()
            self.captions.append(payload)

        await self._room.connect(self._url, self._token, rtc.RoomOptions(auto_subscribe=True))

    async def _drain(self, stream: "rtc.AudioStream") -> None:
        async for event in stream:
            pcm = event.frame.data.tobytes()
            now = time.time()
            level = rms(pcm)
            self.times.append(now)
            self.levels.append(level)
            self.peak_rms = max(self.peak_rms, level)
            if level > SILENCE_RMS and self.first_audio_at is None:
                self.first_audio_at = now
            if self._post is not None:
                self._post.append(pcm)
                if len(self._post) >= self._capture_frames:
                    self.switch_clip = b"".join(self._ring) + b"".join(self._post)
                    self._post = None
            else:
                self._ring.append(pcm)

    def save_switch_clip(self, path: str) -> bool:
        if not self.switch_clip:
            return False
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(LISTEN_RATE)
            w.writeframes(self.switch_clip)
        return True

    async def aclose(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._room.disconnect()


# ---- 분석 ----
def delivery_gaps(times: list[float], threshold: float) -> list[tuple[float, float]]:
    """프레임 도착 간격이 threshold 를 넘은 지점 목록 — (시각, 간격초)."""
    out = []
    for prev, cur in zip(times, times[1:]):
        if cur - prev > threshold:
            out.append((prev, cur - prev))
    return out


def silence_runs(times: list[float], levels: list[float], t0: float, t1: float,
                 threshold: float = SILENCE_RMS) -> list[tuple[float, float, float]]:
    """[t0, t1] 안에서 음성과 음성 사이에 낀 무음 구간 — (시작, 끝, 길이ms).

    앞뒤가 모두 음성인 구간만 센다(구간 시작 전 무음·끝난 뒤 무음은 공백이 아니다).
    """
    runs: list[tuple[float, float, float]] = []
    seen_voice = False
    count = 0
    start: float | None = None
    for t, lv in zip(times, levels):
        if t < t0 or t > t1:
            continue
        if lv > threshold:
            if seen_voice and count:
                runs.append((start, t, count * FRAME_MS))
            seen_voice = True
            count = 0
            start = None
        elif seen_voice:
            if count == 0:
                start = t
            count += 1
    return runs


def summarize(runs: list[tuple[float, float, float]]) -> dict:
    if not runs:
        return {"count": 0, "max_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0}
    lengths = sorted(r[2] for r in runs)
    idx = min(len(lengths) - 1, int(0.95 * len(lengths)))
    return {
        "count": len(runs),
        "max_ms": lengths[-1],
        "p95_ms": lengths[idx],
        "mean_ms": sum(lengths) / len(lengths),
    }


def caption_report(captions: list[dict]) -> dict:
    """자막 seq 연속성과 utterance_id 채번을 점검한다(계약 §6c v1.7)."""
    seqs = [c.get("seq") for c in captions if isinstance(c.get("seq"), int)]
    breaks = []
    for prev, cur in zip(seqs, seqs[1:]):
        if cur != prev + 1:
            breaks.append((prev, cur))
    per_kind: dict[str, dict] = {}
    for kind in ("source", "target"):
        ids: list[int] = []
        for c in captions:
            if c.get("kind") != kind:
                continue
            uid = c.get("utterance_id")
            if uid is None:
                continue
            if not ids or ids[-1] != uid:
                ids.append(uid)
        regressions = [(a, b) for a, b in zip(ids, ids[1:]) if b <= a]
        per_kind[kind] = {
            "utterances": len(ids),
            "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
            "missing_field": sum(
                1 for c in captions if c.get("kind") == kind and c.get("utterance_id") is None
            ),
            "regressions": regressions[:5],
        }
    return {
        "packets": len(captions),
        "seq_first": seqs[0] if seqs else None,
        "seq_last": seqs[-1] if seqs else None,
        "seq_breaks": breaks[:10],
        "seq_break_count": len(breaks),
        "kinds": per_kind,
    }


async def poller(base: str, password: str, channel_id: int, pub: LongRunPublisher,
                 listener: LongRunListener, plan: dict, out: dict, session_t0: float) -> None:
    """워커 상태를 주기적으로 읽어 교체를 감지하고 연속 발화 구간을 켠다/끈다.

    구간 판정은 워커가 보고하는 `session_age_seconds` 가 아니라 **경과 실시간**으로 한다 —
    교체 순간 세션 나이가 0 으로 되감기므로(첫 런에서 확인) 나이를 쓰면 교체 직후
    연속 발화가 꺼져 정작 비교가 필요한 교체 직후 구간이 무음으로 덮인다.
    나이는 기록만 하고(되감김이 곧 교체의 교차 증거다) 판정에는 쓰지 않는다.
    """
    while True:
        # urllib 은 블로킹이라 이벤트 루프에서 직접 부르면 프레임 도착 시각이 밀린다
        # (공백 측정이 곧 판정 근거이므로 타임스탬프 정확도를 지킨다) — 스레드로 뺀다.
        status, body = await asyncio.to_thread(
            call, base, f"/ai-channels/{channel_id}/status", "GET", None, password
        )
        now = time.time()
        worker = (body or {}).get("worker") or {}
        age = worker.get("session_age_seconds")
        row = {
            "t": now,
            "http": status,
            "age": age,
            "running": worker.get("running"),
            "renewals": worker.get("renewals"),
            "restarts": worker.get("restart_count"),
            "seq": worker.get("seq"),
            "caption_seq": worker.get("caption_seq"),
            "last_error": worker.get("last_error"),
            "gate": worker.get("gate"),
        }
        out["polls"].append(row)

        renewals = worker.get("renewals") or 0
        if renewals > out["renewals_seen"]:
            prev_t = out["polls"][-2]["t"] if len(out["polls"]) > 1 else now
            out["switches"].append({"detected_at": now, "lower_bound": prev_t, "n": renewals})
            out["renewals_seen"] = renewals
            listener.arm_switch_capture()
            print(f"[SWITCH] renewals {renewals} 감지 — {now:.3f} (하한 {prev_t:.3f})", flush=True)

        elapsed = now - session_t0
        row["elapsed"] = elapsed
        in_baseline = plan["baseline_from"] <= elapsed <= plan["baseline_to"]
        in_renewal = plan["renewal_from"] <= elapsed <= plan["renewal_to"]
        want = in_baseline or in_renewal
        if want != pub.continuous:
            pub.continuous = want
            label = "기준" if in_baseline else ("교체" if in_renewal else "-")
            print(f"[MODE] 연속 발화 {'ON' if want else 'OFF'} ({label}) "
                  f"elapsed={elapsed:.0f}s age={age if age is None else round(age)}s", flush=True)

        fast = plan["renewal_from"] <= elapsed <= plan["renewal_to"]
        await asyncio.sleep(FAST_POLL_SECONDS if fast else SLOW_POLL_SECONDS)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8880/api",
                    help="Caddy /api 프리픽스를 포함한 field-api base")
    ap.add_argument("--send-password", required=True)
    ap.add_argument("--wav", required=True, help="원음 WAV(mono 16-bit)")
    ap.add_argument("--target-language", default="ko")
    ap.add_argument("--minutes", type=float, default=67.0, help="총 송출 시간(분)")
    ap.add_argument("--silence-seconds", type=float, default=30.0, help="클립 사이 무음(비용 절감)")
    ap.add_argument("--continuous-gap", type=float, default=0.4, help="연속 모드 클립 간격")
    ap.add_argument("--baseline-from", type=float, default=20.0, help="기준 연속 구간 시작(분)")
    ap.add_argument("--baseline-span", type=float, default=4.5, help="연속 구간 길이(분)")
    ap.add_argument("--renewal-from", type=float, default=53.5, help="교체 연속 구간 시작(분)")
    ap.add_argument("--renewal-span", type=float, default=4.5)
    ap.add_argument("--out-json", default="/tmp/t6_longrun.json")
    ap.add_argument("--out-wav", default="/tmp/t6_switch.wav")
    args = ap.parse_args()

    pcm, rate = load_wav(args.wav)
    plan = {
        "baseline_from": args.baseline_from * 60,
        "baseline_to": (args.baseline_from + args.baseline_span) * 60,
        "renewal_from": args.renewal_from * 60,
        "renewal_to": (args.renewal_from + args.renewal_span) * 60,
    }
    print(f"원음 {len(pcm) / (rate * 2):.1f}초 · 목표 {args.target_language} · "
          f"총 {args.minutes:.0f}분 · 무음 {args.silence_seconds:.0f}초", flush=True)

    status, _ = call(args.base, "/channels", "POST",
                     {"channel_id": 0, "language": "en", "label": "Floor(T6)"},
                     args.send_password)
    print(f"[1] Floor 채널: {status}", flush=True)
    status, pubtok = call(args.base, "/publish-tokens", "POST", {"channel_id": 0},
                          args.send_password)
    if status != 200:
        print(f"송신 토큰 실패 {status}: {pubtok}", flush=True)
        return 1
    print(f"[2] 송신 토큰: {pubtok['identity']} → {pubtok['track_name']}", flush=True)

    publisher = LongRunPublisher(pubtok["url"], pubtok["token"], pubtok["track_name"], pcm, rate,
                                 args.silence_seconds, args.continuous_gap)
    await publisher.connect()
    pump = asyncio.create_task(publisher.pump(warmup_seconds=3.0))
    await asyncio.sleep(1.0)

    out: dict = {"polls": [], "switches": [], "renewals_seen": 0, "started_at": time.time()}
    ai_channel_id = None
    listener = None
    poll_task = None
    try:
        status, ai = call(args.base, "/ai-channels", "POST",
                          {"target_language": args.target_language, "source_channel": 0},
                          args.send_password)
        if status != 201:
            print(f"[3] AI 채널 개설 실패 {status}: {ai}", flush=True)
            return 1
        ai_channel_id = ai["channel_id"]
        session_t0 = time.time()
        out["session_t0"] = session_t0
        print(f"[3] AI 채널 {ai_channel_id}({ai['track_name']}) 개설", flush=True)

        status, sub = call(args.base, f"/channels/{ai_channel_id}/subscribe-tokens", "POST")
        if status != 200:
            print(f"[4] 수신 토큰 실패 {status}: {sub}", flush=True)
            return 1
        listener = LongRunListener(sub["url"], sub["token"], sub["track_name"],
                                   ring_seconds=45.0, capture_seconds=45.0)
        await listener.connect()
        print("[4] 청취자 접속", flush=True)

        poll_task = asyncio.create_task(
            poller(args.base, args.send_password, ai_channel_id, publisher, listener, plan, out,
                   session_t0)
        )

        deadline = time.time() + args.minutes * 60
        while time.time() < deadline:
            await asyncio.sleep(60)
            elapsed = (time.time() - session_t0) / 60
            last = out["polls"][-1] if out["polls"] else {}
            print(f"[..] {elapsed:5.1f}분 · 클립 {publisher.clips} · "
                  f"프레임 {len(listener.times)} · 자막 {len(listener.captions)} · "
                  f"renewals={last.get('renewals')} restarts={last.get('restarts')} "
                  f"seq={last.get('seq')}", flush=True)
        print("[5] 송출 종료 — 잔여 수집 20초", flush=True)
        await asyncio.sleep(20)
    finally:
        if poll_task is not None:
            poll_task.cancel()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if listener is not None:
            out["frames"] = len(listener.times)
            out["captions"] = caption_report(listener.captions)
            _analyze(listener, publisher, out, args, plan)
            await listener.aclose()
        await publisher.aclose()
        if ai_channel_id is not None:
            code, _ = call(args.base, f"/ai-channels/{ai_channel_id}", "DELETE",
                           password=args.send_password)
            print(f"[6] AI 채널 삭제: {code}", flush=True)
            code, body = call(args.base, f"/channels/{ai_channel_id}", "GET",
                              password=args.send_password)
            print(f"[6b] 종료 판정 /channels/{ai_channel_id}: "
                  f"{code} state={(body or {}).get('state')}", flush=True)
        call(args.base, "/channels/0", "DELETE", password=args.send_password)
        with open(args.out_json, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)
        print(f"[7] 타임라인 저장: {args.out_json}", flush=True)
    return 0


def _analyze(listener: LongRunListener, publisher: LongRunPublisher, out: dict,
             args, plan: dict) -> None:
    """수집된 타임라인으로 교체 구간 공백을 판정한다(기준 구간과 비교)."""
    times, levels = listener.times, listener.levels
    if not times:
        print("!! 수신 프레임 없음 — 분석 불가", flush=True)
        return
    t0 = out.get("session_t0", times[0])
    gaps = delivery_gaps(times, DELIVERY_GAP_SECONDS)
    out["delivery_gaps"] = [{"at": g[0], "rel": g[0] - t0, "seconds": g[1]} for g in gaps[:50]]
    out["delivery_gap_count"] = len(gaps)

    def window(label: str, a: float, b: float) -> dict:
        runs = silence_runs(times, levels, a, b)
        s = summarize(runs)
        s["label"] = label
        s["from_rel"] = a - t0
        s["to_rel"] = b - t0
        s["runs"] = [
            {"rel": r[0] - t0, "ms": r[2]} for r in runs if r[2] >= REPORT_GAP_MS
        ][:60]
        return s

    windows = [window("baseline", t0 + plan["baseline_from"], t0 + plan["baseline_to"])]
    if out["switches"]:
        sw = out["switches"][0]
        s_at, s_lb = sw["detected_at"], sw["lower_bound"]
        windows.append(window("pre_switch", s_at - 120, s_lb))
        windows.append(window("switch", s_lb - 5, s_at + 30))
        windows.append(window("post_switch", s_at + 30, s_at + 150))
        # 교체 순간(하한~감지)을 품는 무음 구간이 있으면 그것이 곧 "교체로 생긴 공백"이다.
        spanning = [
            {"rel": r[0] - t0, "ms": r[2]}
            for r in silence_runs(times, levels, s_lb - 30, s_at + 60)
            if r[0] <= s_at and r[1] >= s_lb
        ]
        out["switch_spanning_gaps"] = spanning
    out["windows"] = windows
    out["publisher"] = {
        "clips": publisher.clips,
        "speech_seconds": publisher.speech_seconds,
        # 원음이 실제로 나가던 시간대 — 출력 무음이 "입력이 없어서"인지 "교체 때문"인지
        # 사후에 가르는 근거다(교체 판정의 필수 대조군).
        "speech_spans": [(round(a, 3), round(b, 3)) for a, b in publisher.speech_spans],
    }
    if listener.save_switch_clip(args.out_wav):
        out["switch_wav"] = args.out_wav
        print(f"[분석] 교체 전후 오디오 저장: {args.out_wav}", flush=True)

    print("─" * 64, flush=True)
    print(f"수신 프레임 {len(times)} · peak RMS {listener.peak_rms:.0f} · "
          f"전달 공백(>{DELIVERY_GAP_SECONDS}s) {len(gaps)}건", flush=True)
    for w in windows:
        print(f"  [{w['label']:11s}] 무음구간 {w['count']:3d}건 · "
              f"max {w['max_ms']:7.0f}ms · p95 {w['p95_ms']:7.0f}ms · "
              f"mean {w['mean_ms']:6.0f}ms", flush=True)
    if out.get("switch_spanning_gaps"):
        for g in out["switch_spanning_gaps"]:
            print(f"  !! 교체 순간을 품은 무음 {g['ms']:.0f}ms (rel {g['rel']:.1f}s)", flush=True)
    else:
        print("  교체 순간을 품은 무음 구간 없음", flush=True)
    print(f"  자막: {out['captions']}", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
