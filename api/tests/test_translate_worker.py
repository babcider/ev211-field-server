# 번역 워커 단위 테스트 — 순수 헬퍼·원음→번역 파이프라인·슈퍼바이저 재시작(OpenAI·LiveKit mock)
from __future__ import annotations

import asyncio
import base64

import pytest

from app.translate_worker import (
    AiChannelManager,
    AiTranslateChannel,
    AiWorkerParams,
    TranslateSessionError,
    TranslateWorker,
    decode_output_delta,
    output_frame_samples,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _run_recv(worker) -> None:
    """활성 세션 reader(세션→공용 큐) + 소비 루프를 함께 돌린다(다중 세션 구조).

    make-before-break 이후 수신은 '세션별 reader → 공용 큐 → 단일 소비 루프' 구조라,
    소비 루프만 단독으로 돌리면 큐가 비어 영원히 대기한다.
    """

    async def body():
        worker._start_reader(worker._session)
        try:
            await asyncio.wait_for(worker._recv_loop(), timeout=5)
        finally:
            for t in worker._reader_tasks:
                t.cancel()
            await asyncio.gather(*worker._reader_tasks, return_exceptions=True)

    _run(body())


def _params(channel_id: int = 1) -> AiWorkerParams:
    return AiWorkerParams(
        channel_id=channel_id,
        target_language="en",
        rtc_url="ws://livekit:7880",
        token="jwt-token",
        room="field-g1",
        publish_track=f"ch-{channel_id:02d}",
        subscribe_track="ch-00",
    )


# ---- fake I/O ----
class FakeAudioFrame:
    """rtc.AudioFrame 생성자 인자만 기록하는 대역(네이티브 FFI 회피)."""

    def __init__(self, data, sample_rate, num_channels, samples_per_channel):
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class FakeSource:
    def __init__(self):
        self.frames = []

    async def capture_frame(self, frame):
        self.frames.append(frame)


class FakeSession:
    """gpt-realtime-translate 세션 대역. events 로 미리 정한 이벤트 시퀀스를 낸다."""

    def __init__(self, events=None):
        self._events = events or []
        self.connected_language = None
        self.appended = []
        self.closed = False

    async def connect(self, target_language):
        self.connected_language = target_language

    async def append_audio(self, pcm24k):
        self.appended.append(pcm24k)

    async def events(self):
        for e in self._events:
            yield e

    async def aclose(self):
        self.closed = True


# ---- 순수 헬퍼 ----
def test_decode_output_delta_roundtrip():
    raw = b"\x01\x00\x02\x00\x03\x00"
    assert decode_output_delta(base64.b64encode(raw).decode()) == raw


def test_output_frame_samples_mono_pcm16():
    # 6바이트 mono PCM16 = 3 표본.
    assert output_frame_samples(b"\x00\x00\x11\x11\x22\x22") == 3
    assert output_frame_samples(b"") == 0


# ---- 원음→번역 파이프라인 ----
def test_output_audio_delta_emits_capture_frame(monkeypatch):
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    source = FakeSource()
    worker._source = source
    pcm = b"\x0a\x00\x0b\x00"  # 2 표본
    ev = {"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()}

    _run(worker._handle_event(ev))

    assert len(source.frames) == 1
    frame = source.frames[0]
    assert frame.data == pcm
    assert frame.sample_rate == 24_000
    assert frame.num_channels == 1
    assert frame.samples_per_channel == 2
    assert worker.seq == 1
    assert worker.last_audio_at is not None


def test_empty_delta_is_ignored(monkeypatch):
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    worker._source = FakeSource()
    _run(worker._handle_event({"type": "session.output_audio.delta", "delta": ""}))
    assert worker.seq == 0
    assert worker._source.frames == []


def test_transcript_deltas_go_to_callback():
    seen = []
    worker = TranslateWorker(
        _params(),
        session_factory=lambda: FakeSession(),
        on_transcript=lambda kind, text: seen.append((kind, text)),
    )
    _run(worker._handle_event({"type": "session.output_transcript.delta", "delta": "hello"}))
    _run(worker._handle_event({"type": "session.input_transcript.delta", "delta": "안녕"}))
    assert seen == [("target", "hello"), ("source", "안녕")]


def test_recv_loop_processes_event_stream(monkeypatch):
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    pcm = base64.b64encode(b"\x01\x00\x02\x00").decode()
    events = [
        {"type": "session.output_audio.delta", "delta": pcm},
        {"type": "session.output_transcript.delta", "delta": "hi"},
        {"type": "session.output_audio.delta", "delta": pcm},
        {"type": "session.closed"},
    ]
    seen = []
    worker = TranslateWorker(
        _params(),
        session_factory=lambda: FakeSession(),
        on_transcript=lambda kind, text: seen.append((kind, text)),
    )
    worker._source = FakeSource()
    worker._session = FakeSession(events)

    _run_recv(worker)

    assert len(worker._source.frames) == 2  # 오디오 델타 2건만 발행
    assert worker.seq == 2
    assert seen == [("target", "hi")]


def test_resample_passthrough_when_already_24k():
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    frame = FakeAudioFrame_with_data(b"\xaa\xbb", 24_000)
    out = worker._resample_to_24k(frame)
    assert out == [b"\xaa\xbb"]


class FakeAudioFrame_with_data:
    """sample_rate·data.tobytes() 만 제공하는 리샘플 입력 대역."""

    def __init__(self, raw: bytes, sample_rate: int):
        self._raw = raw
        self.sample_rate = sample_rate

    @property
    def data(self):
        return _Bytesish(self._raw)


class _Bytesish:
    def __init__(self, raw):
        self._raw = raw

    def tobytes(self):
        return self._raw


# ---- 자막 data 패킷 발행 ----
class FakeLocalParticipant:
    def __init__(self, fail: bool = False):
        self.published = []
        self._fail = fail

    async def publish_data(self, payload, reliable=True, topic=""):
        if self._fail:
            raise RuntimeError("data channel down")
        self.published.append((payload, reliable, topic))


class FakeRoom:
    def __init__(self, fail: bool = False):
        self.local_participant = FakeLocalParticipant(fail)
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def test_transcript_delta_publishes_caption_data_packet():
    import json as _json

    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    room = FakeRoom()
    worker._room = room

    _run(worker._handle_event({"type": "session.output_transcript.delta", "delta": "안녕"}))
    _run(worker._handle_event({"type": "session.input_transcript.delta", "delta": "hello"}))

    assert len(room.local_participant.published) == 2
    payload, reliable, topic = room.local_participant.published[0]
    body = _json.loads(payload.decode())
    assert topic == "captions" and reliable is True
    assert body["kind"] == "target"
    assert body["language"] == "en"  # _params 의 target_language
    assert body["delta"] == "안녕"
    assert body["channel_id"] == 1 and body["track_name"] == "ch-01"
    assert body["seq"] == 1
    # 원문 자막은 언어 표기 없이 source 로 구분한다.
    src = _json.loads(room.local_participant.published[1][0].decode())
    assert src["kind"] == "source" and src["language"] is None and src["seq"] == 2
    assert worker.caption_seq == 2


def test_caption_publish_failure_does_not_break_pipeline(monkeypatch):
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    seen = []
    worker = TranslateWorker(
        _params(),
        session_factory=lambda: FakeSession(),
        on_transcript=lambda kind, text: seen.append((kind, text)),
    )
    worker._room = FakeRoom(fail=True)
    worker._source = FakeSource()

    # 자막 발행이 실패해도 이벤트 처리는 계속되고 오디오 경로는 영향받지 않는다.
    assert _run(worker._handle_event({"type": "session.output_transcript.delta", "delta": "x"})) is True
    pcm = base64.b64encode(b"\x01\x00").decode()
    _run(worker._handle_event({"type": "session.output_audio.delta", "delta": pcm}))
    assert seen == [("target", "x")]
    assert worker.seq == 1
    assert worker._caption_failed is True


def test_empty_caption_delta_is_ignored():
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    worker._room = FakeRoom()
    _run(worker._handle_event({"type": "session.output_transcript.delta", "delta": ""}))
    assert worker.caption_seq == 0
    assert worker._room.local_participant.published == []


# ---- make-before-break 세션 갱신 ----
def test_renew_session_switches_atomically_and_drains_old(monkeypatch):
    import app.translate_worker as tw

    monkeypatch.setattr(tw, "AI_SESSION_DRAIN_SECONDS", 0.0)

    async def body():
        old = FakeSession()
        new = FakeSession()
        worker = TranslateWorker(_params(), session_factory=lambda: new)
        worker._session = old
        worker.session_started_at = 0.0

        await worker._renew_session()

        # 활성 세션이 원자적으로 교체되고(새 세션은 이미 connect 완료), 구세션은 드레인 대상.
        assert worker._session is new
        assert new.connected_language == "en"
        assert worker.renewals == 1
        assert worker.renewal_due is False  # session_started_at 갱신됨
        assert old.closed is False  # 스위치 직후에는 아직 살아 있다(잔여 출력 수거)
        await asyncio.gather(*worker._pump_tasks)
        assert old.closed is True
        assert worker._draining == set()

    _run(body())


def test_renew_failure_keeps_old_session():
    class DeadSession(FakeSession):
        async def connect(self, target_language):
            raise RuntimeError("openai down")

    async def body():
        old = FakeSession()
        worker = TranslateWorker(_params(), session_factory=lambda: DeadSession())
        worker._session = old

        with pytest.raises(RuntimeError):
            await worker._renew_session()

        # 워밍 실패 시 활성 세션은 구세션 그대로 — 오디오가 끊기지 않는다.
        assert worker._session is old
        assert old.closed is False
        assert worker.renewals == 0

    _run(body())


def test_stale_session_audio_is_emitted_and_its_end_ignored(monkeypatch):
    """스위치된 구세션의 잔여 오디오는 계속 발행하고, 구세션 종료는 루프를 끝내지 않는다."""
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    pcm = base64.b64encode(b"\x01\x00").decode()

    async def body():
        active = FakeSession([{"type": "session.closed"}])
        stale = FakeSession([{"type": "session.output_audio.delta", "delta": pcm}])
        worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
        worker._source = FakeSource()
        worker._session = active
        # 구세션 이벤트를 먼저 큐에 넣고, 그 뒤 활성 세션 종료가 소비 루프를 끝낸다.
        worker._start_reader(stale)
        await asyncio.sleep(0)
        worker._start_reader(active)
        await asyncio.wait_for(worker._recv_loop(), timeout=5)

        assert len(worker._source.frames) == 1  # 구세션 잔여 오디오도 발행됨
        for t in worker._reader_tasks:
            t.cancel()
        await asyncio.gather(*worker._reader_tasks, return_exceptions=True)

    _run(body())


def test_stale_session_error_does_not_crash_worker(monkeypatch):
    async def body():
        active = FakeSession([{"type": "session.closed"}])
        worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
        worker._session = active
        # 구세션의 OpenAI error 이벤트는 무시된다(활성 세션이 아니므로 재시작 사유가 아님).
        await worker._event_q.put(
            (FakeSession(), {"type": "error", "error": {"message": "stale"}}, None)
        )
        await worker._event_q.put((FakeSession(), None, RuntimeError("stale ws closed")))
        worker._start_reader(active)
        await asyncio.wait_for(worker._recv_loop(), timeout=5)
        for t in worker._reader_tasks:
            t.cancel()
        await asyncio.gather(*worker._reader_tasks, return_exceptions=True)

    _run(body())


# ---- 세션 갱신 훅 ----
def test_renewal_due_flips_after_threshold(monkeypatch):
    import app.translate_worker as tw

    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    assert worker.renewal_due is False  # 세션 미개설
    worker.session_started_at = 0.0
    monkeypatch.setattr(tw.time, "time", lambda: tw.AI_SESSION_RENEW_SECONDS + 1)
    assert worker.renewal_due is True


# ---- 슈퍼바이저 ----
class BoomWorker:
    async def run(self, stop):
        raise RuntimeError("boom")

    async def aclose(self):
        pass


class BlockingWorker:
    session_age_seconds = None
    last_audio_at = None
    last_floor_frame_at = None
    seq = 0
    caption_seq = 0
    renewals = 0
    renewal_due = False

    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def run(self, stop):
        self.started.set()
        try:
            await stop.wait()
        finally:
            # 실제 워커처럼 run 의 finally 에서 세션·룸을 정리한다(취소로 실행됨).
            await self.aclose()

    async def aclose(self):
        self.closed = True


def test_supervisor_restarts_on_crash_then_runs():
    async def body():
        good = BlockingWorker()
        seq = iter([BoomWorker(), BoomWorker(), good])
        channel = AiTranslateChannel(
            1, "en", worker_factory=lambda: next(seq), backoff_base=0.0, max_backoff=0.0
        )
        channel.start()
        await asyncio.wait_for(good.started.wait(), timeout=2)
        assert channel.restart_count == 2
        assert channel.last_error is not None and "boom" in channel.last_error
        status = channel.status()
        assert status["running"] is True
        assert status["restart_count"] == 2
        # server-first 종료 — 현재 워커 aclose + 감독 task 수거.
        await channel.stop()
        assert good.closed is True
        assert channel._task.done()

    _run(body())


def test_manager_start_stop_status():
    async def body():
        good = BlockingWorker()
        manager = AiChannelManager(
            worker_factory=lambda params: good, backoff_base=0.0, max_backoff=0.0
        )
        channel = manager.start(_params(3))
        await asyncio.wait_for(good.started.wait(), timeout=2)
        assert manager.has(3) is True
        assert manager.status(3)["channel_id"] == 3
        assert manager.status(99) is None
        assert await manager.stop(3) is True
        assert manager.has(3) is False
        assert await manager.stop(3) is False  # 이미 없음
        assert good.closed is True
        # 채널 참조도 정지 상태.
        assert channel._task.done()

    _run(body())


# ---- HIGH1: 매 접속 fresh 토큰 프로바이더 ----
def test_resolve_token_prefers_provider_each_connect():
    async def body():
        calls = []

        async def provider():
            calls.append(1)
            return f"fresh-{len(calls)}"

        worker = TranslateWorker(
            _params(), session_factory=lambda: FakeSession(), token_provider=provider
        )
        assert await worker._resolve_token() == "fresh-1"
        assert await worker._resolve_token() == "fresh-2"  # 재접속마다 새 토큰

    _run(body())


def test_resolve_token_falls_back_to_params_token():
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    assert _run(worker._resolve_token()) == "jwt-token"


# ---- MED5: OpenAI error·session.closed 종료 ----
def test_error_event_raises_session_error():
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    with pytest.raises(TranslateSessionError):
        _run(worker._handle_event({"type": "error", "error": {"message": "bad key"}}))


def test_session_closed_stops_recv_loop(monkeypatch):
    monkeypatch.setattr("app.translate_worker.rtc.AudioFrame", FakeAudioFrame)
    pcm = base64.b64encode(b"\x01\x00").decode()
    events = [
        {"type": "session.output_audio.delta", "delta": pcm},
        {"type": "session.closed"},
        {"type": "session.output_audio.delta", "delta": pcm},  # closed 이후는 소비 안 함
    ]
    worker = TranslateWorker(_params(), session_factory=lambda: FakeSession())
    worker._source = FakeSource()
    worker._session = FakeSession(events)
    _run_recv(worker)
    assert len(worker._source.frames) == 1  # session.closed 에서 즉시 종료


# ---- MED6: 취소·종료 시 pump 태스크·AudioStream·세션 수거 ----
class _FakeStream:
    def __init__(self):
        self.closed = False
        self._never = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._never.wait()  # 프레임 없이 영구 대기(취소 대상)
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _FakeLocalParticipant:
    async def publish_track(self, track, opts):
        pass


class _FakeRoom:
    """connect 시 track_subscribed 를 즉시 발화해 Floor pump 태스크를 만드는 룸 대역."""

    def __init__(self):
        self._handlers = {}
        self.local_participant = _FakeLocalParticipant()
        self.remote_participants = {}  # 접속 시점 기존 발행 트랙 순회 대상(결함3 — 비어 있음)
        self.disconnected = False
        self.connect_token = None

    def on(self, name):
        def deco(fn):
            self._handlers[name] = fn
            return fn

        return deco

    async def connect(self, url, token, options):
        self.connect_token = token
        from livekit import rtc

        pub = type("Pub", (), {"name": "ch-00"})()
        track = type("Trk", (), {"kind": rtc.TrackKind.KIND_AUDIO})()
        self._handlers["track_subscribed"](track, pub, object())

    async def disconnect(self):
        self.disconnected = True


def test_run_cleans_up_pump_tasks_and_streams(monkeypatch):
    from livekit import rtc

    made_streams = []
    monkeypatch.setattr(rtc, "AudioSource", lambda *a, **k: object())
    monkeypatch.setattr(
        rtc.LocalAudioTrack, "create_audio_track", lambda name, source: object()
    )
    monkeypatch.setattr(rtc, "RoomOptions", lambda **k: object())
    monkeypatch.setattr(rtc, "TrackPublishOptions", type("TPO", (), {}))
    monkeypatch.setattr(
        rtc, "TrackSource", type("TS", (), {"SOURCE_MICROPHONE": 1})
    )

    def make_stream(*a, **k):
        s = _FakeStream()
        made_streams.append(s)
        return s

    monkeypatch.setattr(rtc, "AudioStream", make_stream)

    async def body():
        session = FakeSession()
        room = _FakeRoom()
        worker = TranslateWorker(
            _params(),
            session_factory=lambda: session,
            room_factory=lambda: room,
        )
        stop = asyncio.Event()
        stop.set()  # 접속 직후 즉시 종료 → finally 정리 경로 검증
        await worker.run(stop)
        # pump 태스크 취소·수거 + AudioStream 닫힘 + 세션·룸 정리.
        assert made_streams and made_streams[0].closed is True
        assert worker._pump_tasks and worker._pump_tasks[0].done()
        assert session.closed is True
        assert room.disconnected is True

    _run(body())


# ---- MED7: close 는 한 워커 aclose hang 에도 타임아웃으로 반환 ----
def test_manager_close_times_out_on_hanging_worker(monkeypatch):
    import app.translate_worker as tw

    monkeypatch.setattr(tw, "AI_WORKER_STOP_TIMEOUT_SECONDS", 0.2)

    class HangWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False

        def __init__(self):
            self.started = asyncio.Event()

        async def run(self, stop):
            self.started.set()
            await stop.wait()

        async def aclose(self):
            await asyncio.Event().wait()  # 영구 hang

    async def body():
        hw = HangWorker()
        manager = AiChannelManager(
            worker_factory=lambda params: hw, backoff_base=0.0, max_backoff=0.0
        )
        manager.start(_params(4))
        await asyncio.wait_for(hw.started.wait(), timeout=2)
        # aclose 가 hang 해도 close 는 타임아웃으로 반환한다(녹음·DB 정리 블록 방지).
        await asyncio.wait_for(manager.close(), timeout=2)
        assert manager.has(4) is False

    _run(body())


# ---- 회귀4: token_provider 는 첫 연결에 미호출, 재접속에만 호출 ----
def test_token_provider_only_on_reconnect():
    async def body():
        captured = []

        async def prov():
            return "fresh-token"

        class RecordingWorker:
            session_age_seconds = None
            last_audio_at = None
            seq = 0
            renewal_due = False

            def __init__(self):
                self.token_provider = "UNSET"
                self.ready = None
                self.started = asyncio.Event()

            async def run(self, stop):
                captured.append(self.token_provider)  # 슈퍼바이저가 주입한 값 기록
                if len(captured) == 1:
                    raise RuntimeError("first crash → 재접속 유도")
                self.started.set()
                await stop.wait()

            async def aclose(self):
                pass

        w1, w2 = RecordingWorker(), RecordingWorker()
        it = iter([w1, w2])
        channel = AiTranslateChannel(
            1, "en", worker_factory=lambda: next(it),
            backoff_base=0.0, max_backoff=0.0, token_provider=prov,
        )
        channel.start()
        await asyncio.wait_for(w2.started.wait(), timeout=2)
        assert captured[0] is None  # 첫 연결: 최초 토큰 사용(provider 미주입)
        assert captured[1] is prov  # 재접속: fresh 토큰 프로바이더 주입
        await channel.stop()

    _run(body())


# ---- 회귀3: stop 타임아웃 시 감독 태스크 참조 유지 + close 가 마지막에 수거 ----
def test_stop_timeout_retains_then_reaps_leaked_task(monkeypatch):
    import app.translate_worker as tw

    monkeypatch.setattr(tw, "AI_WORKER_STOP_TIMEOUT_SECONDS", 0.2)

    class StuckWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False

        def __init__(self):
            self.started = asyncio.Event()
            self.token_provider = None
            self.ready = None

        async def run(self, stop):
            self.started.set()
            try:
                await asyncio.Event().wait()  # stop 무시, 취소만 반응
            except asyncio.CancelledError:
                await asyncio.Event().wait()  # 취소 후에도 정리 고착

        async def aclose(self):
            await asyncio.Event().wait()  # aclose 고착

    async def body():
        sw = StuckWorker()
        manager = AiChannelManager(
            worker_factory=lambda params: sw, backoff_base=0.0, max_backoff=0.0
        )
        ch = manager.start(_params(7))
        await asyncio.wait_for(sw.started.wait(), timeout=2)
        # stop 이 타임아웃 → 활성 레지스트리에선 빠지지만 태스크 참조는 leaked 로 보관.
        assert await asyncio.wait_for(manager.stop(7, timeout=0.2), timeout=3) is True
        assert manager.has(7) is False
        assert ch in manager._leaked
        assert not ch._task.done()  # 정리 고착 — 아직 살아 있음(참조 유지)
        # close 의 마지막 수거가 강제 취소로 확실히 종료한다.
        await asyncio.wait_for(manager.close(), timeout=3)
        assert ch._task.done()
        assert ch not in manager._leaked

    _run(body())


# ---- 결함1: stop 은 단일 deadline 만 쓴다(취소 무시 정리에도 2배·무한대기 없음) ----
def test_stop_uses_single_hard_deadline(monkeypatch):
    import time as _time

    class IgnoreCancelWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False

        def __init__(self):
            self.started = asyncio.Event()
            self.token_provider = None
            self.ready = None

        async def run(self, stop):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()  # 취소를 삼키고 재대기(하드 타임아웃 함정)

        async def aclose(self):
            await asyncio.Event().wait()  # 호출되면 고착(새 stop 은 호출하지 않는다)

    async def body():
        w = IgnoreCancelWorker()
        channel = AiTranslateChannel(
            1, "en", worker_factory=lambda: w, backoff_base=0.0, max_backoff=0.0
        )
        channel.start()
        await asyncio.wait_for(w.started.wait(), timeout=2)
        t0 = _time.monotonic()
        done = await channel.stop(timeout=0.3)
        elapsed = _time.monotonic() - t0
        assert done is False  # deadline 초과 → 미완료
        # 단일 deadline(≈0.3s) — aclose+task 순차 2배(0.6s+)도, 무한대기도 아니다.
        assert elapsed < 0.6, elapsed
        assert not channel._task.done()  # 참조 유지(취소 요청됨)
        # 마지막 강제 수거.
        channel._task.cancel()
        await asyncio.wait({channel._task}, timeout=1)

    _run(body())


# ---- 결함3: auto_subscribe=False + 원음(ch-00) 트랙만 선택 구독 ----
def test_selective_subscribe_only_source_track(monkeypatch):
    from livekit import rtc

    captured = {}
    monkeypatch.setattr(rtc, "AudioSource", lambda *a, **k: object())
    monkeypatch.setattr(
        rtc.LocalAudioTrack, "create_audio_track", lambda name, source: object()
    )
    monkeypatch.setattr(
        rtc, "RoomOptions", lambda **k: (captured.update(k), object())[1]
    )
    monkeypatch.setattr(rtc, "TrackPublishOptions", type("TPO", (), {}))
    monkeypatch.setattr(rtc, "TrackSource", type("TS", (), {"SOURCE_MICROPHONE": 1}))

    subscribed = []

    class Pub:
        def __init__(self, name):
            self.name = name
            self.kind = rtc.TrackKind.KIND_AUDIO

        def set_subscribed(self, value):
            subscribed.append(self.name)

    class Part:
        def __init__(self, pubs):
            self.track_publications = {p.name: p for p in pubs}

    class LP:
        async def publish_track(self, track, opts):
            pass

    class Room:
        def __init__(self):
            self._h = {}
            self.local_participant = LP()
            # 접속 시점에 이미 원음(ch-00)과 타 채널(ch-05)이 발행돼 있다.
            self.remote_participants = {"p": Part([Pub("ch-00"), Pub("ch-05")])}

        def on(self, name):
            def deco(fn):
                self._h[name] = fn
                return fn

            return deco

        async def connect(self, url, token, options):
            pass

    async def body():
        room = Room()
        worker = TranslateWorker(
            _params(), session_factory=lambda: FakeSession(), room_factory=lambda: room
        )
        await worker._connect_livekit(asyncio.Queue())
        assert captured.get("auto_subscribe") is False  # 전체 자동 구독 안 함
        assert subscribed == ["ch-00"]  # 기존 발행 중 원음만 선택 구독
        # 이후 track_published 이벤트도 매칭 트랙만 구독한다.
        room._h["track_published"](Pub("ch-00"), object())
        room._h["track_published"](Pub("ch-07"), object())
        assert subscribed == ["ch-00", "ch-00"]  # ch-07 미구독

    _run(body())


# ---- 결함1·A: reap 후에도 살아있는 task 는 _leaked 로 다시 추적된다 ----
def test_reap_reretains_still_alive_task(monkeypatch):
    import app.translate_worker as tw

    monkeypatch.setattr(tw, "AI_WORKER_STOP_TIMEOUT_SECONDS", 0.15)

    class ReluctantWorker:
        session_age_seconds = None
        last_audio_at = None
        seq = 0
        renewal_due = False

        def __init__(self):
            self.started = asyncio.Event()
            self.cancels = 0
            self.token_provider = None
            self.ready = None

        async def run(self, stop):
            self.started.set()
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancels += 1
                    if self.cancels >= 3:
                        raise  # 3번째 취소에만 종료(그 전엔 취소를 삼키고 재대기)

        async def aclose(self):
            pass

    async def body():
        w = ReluctantWorker()
        manager = AiChannelManager(
            worker_factory=lambda params: w, backoff_base=0.0, max_backoff=0.0
        )
        ch = manager.start(_params(9))
        await asyncio.wait_for(w.started.wait(), timeout=2)
        # stop 타임아웃(1번째 취소 삼킴) → 활성 레지스트리에서 빠지고 _leaked 로 추적.
        assert await asyncio.wait_for(manager.stop(9, timeout=0.15), timeout=2) is True
        assert ch in manager._leaked and not ch._task.done()
        # reap(2번째 취소 삼킴) 후에도 살아있으면 다시 _leaked 로 추적 유지(결함1·A 핵심).
        await asyncio.wait_for(manager._reap_leaked(), timeout=2)
        assert ch in manager._leaked and not ch._task.done()  # after_reap: 여전히 추적됨
        # 마지막 reap(3번째 취소)에 종료·추적 제거(무한 성장 방지).
        await asyncio.wait_for(manager._reap_leaked(), timeout=2)
        assert ch._task.done()
        assert ch not in manager._leaked

    _run(body())
