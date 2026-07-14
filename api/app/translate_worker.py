# 모드 C AI 통역 — 목표 언어 1개를 담당하는 gpt-realtime-translate 번역 워커와 슈퍼바이저
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol

from livekit import rtc

from .config import (
    AI_FLOOR_SAMPLE_RATE,
    AI_INPUT_SAMPLE_RATE,
    AI_SESSION_RENEW_SECONDS,
    AI_TRANSLATE_INPUT_MODEL,
    AI_TRANSLATE_URL,
    AI_WORKER_BACKOFF_BASE_SECONDS,
    AI_WORKER_BACKOFF_MAX_SECONDS,
    AI_WORKER_STOP_TIMEOUT_SECONDS,
)

log = logging.getLogger(__name__)

NUM_CHANNELS = 1
_FLOOR_QUEUE_MAX = 200  # Floor 프레임 백프레셔 상한(초과 시 가장 오래된 프레임 폐기)


class TranslateSessionError(RuntimeError):
    """OpenAI 번역 세션 error 이벤트 — 슈퍼바이저가 백오프 후 재접속하도록 유도한다."""


# ---- OpenAI 번역 세션 인터페이스 ----
class TranslateSession(Protocol):
    """gpt-realtime-translate WebSocket 세션의 최소 계약(주입 가능 — 테스트에서 mock)."""

    async def connect(self, target_language: str) -> None: ...

    async def append_audio(self, pcm24k: bytes) -> None: ...

    def events(self) -> AsyncIterator[dict]: ...

    async def aclose(self) -> None: ...


class RealtimeTranslateSession:
    """gpt-realtime-translate 번역 전용 WebSocket 세션(raw WS 직결).

    번역 엔드포인트는 일반 대화용 /v1/realtime 과 스키마가 다르다(OpenAI 조사 근거):
    - 목표 언어는 instructions 가 아니라 session.audio.output.language 로 1회 지정.
    - 오디오는 24kHz PCM16 mono base64. commit·response.create 없이 append 만 계속.
    - 수신 이벤트는 session.* 프리픽스(session.output_audio.delta 등).
    voice/prompt 미지원이라 지정하지 않는다. 세션당 목표 언어 1개(다국어는 세션 분리).
    """

    def __init__(self, api_key: str, connect: Callable | None = None) -> None:
        self._api_key = api_key
        # websockets.connect 를 주입 가능하게 둔다(테스트·라이브러리 버전 대응).
        self._connect = connect
        self._ws = None

    async def connect(self, target_language: str) -> None:
        if self._connect is None:
            import websockets

            self._connect = websockets.connect
        # websockets 신버전은 additional_headers, 구버전은 extra_headers 를 쓴다.
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            self._ws = await self._connect(AI_TRANSLATE_URL, additional_headers=headers)
        except TypeError:
            self._ws = await self._connect(AI_TRANSLATE_URL, extra_headers=headers)
        await self._ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "audio": {
                            "input": {
                                "transcription": {"model": AI_TRANSLATE_INPUT_MODEL},
                                "noise_reduction": {"type": "near_field"},
                            },
                            "output": {"language": target_language},
                        }
                    },
                }
            )
        )

    async def append_audio(self, pcm24k: bytes) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "type": "session.input_audio_buffer.append",
                    "audio": base64.b64encode(pcm24k).decode("ascii"),
                }
            )
        )

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            yield json.loads(raw)

    async def aclose(self) -> None:
        if self._ws is None:
            return
        # 정상 종료: WS 를 즉시 끊기 전에 session.close 를 보내 서버가 잔여 출력을
        # 마무리하게 한다(best-effort). session.closed 드레인(잔여 출력 완전 수거)은
        # make-before-break 와 함께 후속 범위다 — 여기서는 close 프레임 전송까지만 한다.
        try:
            await self._ws.send(json.dumps({"type": "session.close"}))
        except Exception:  # noqa: BLE001 — 종료 신호는 best-effort
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 — 종료는 best-effort
            pass
        self._ws = None


# ---- 순수 로직 헬퍼 ----
def decode_output_delta(delta_b64: str) -> bytes:
    """번역 오디오 델타(base64 24kHz PCM16)를 원시 PCM 바이트로 디코드한다(순수)."""
    return base64.b64decode(delta_b64)


def output_frame_samples(pcm: bytes) -> int:
    """24kHz mono PCM16 바이트 길이에서 표본 수(samples_per_channel)를 계산한다(순수)."""
    return len(pcm) // (2 * NUM_CHANNELS)


# ---- 번역 워커 ----
@dataclass
class AiWorkerParams:
    """AI 통역 워커 1개 기동에 필요한 파라미터 묶음."""

    channel_id: int
    target_language: str
    rtc_url: str
    token: str
    room: str
    publish_track: str  # 워커가 내보낼 번역 트랙명(ch-NN)
    subscribe_track: str  # 워커가 구독할 원음 트랙명(ch-00)


class TranslateWorker:
    """Floor(ch-00) 원음을 구독해 gpt-realtime-translate 로 번역하고 ch-NN 으로 재발행한다.

    I/O(LiveKit Room·OpenAI 세션)는 주입 가능한 팩토리로 받아 단위 테스트에서 mock 한다.
    파이프라인은 두 루프로 분리한다.
    - send: Floor 프레임 → 24kHz 리샘플 → OpenAI append.
    - recv: OpenAI session.output_audio.delta → AudioSource.capture_frame(ch-NN 발행).
    자막(session.output_transcript.delta)은 on_transcript 콜백으로 노출한다(발행은 후속).
    """

    def __init__(
        self,
        params: AiWorkerParams,
        session_factory: Callable[[], TranslateSession],
        on_transcript: Callable[[str, str], None] | None = None,
        room_factory: Callable[[], "rtc.Room"] | None = None,
        token_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._params = params
        self._session_factory = session_factory
        self._on_transcript = on_transcript
        self._room_factory = room_factory or rtc.Room
        # 매 (재)접속 시 fresh publish 토큰(+lease 재획득)을 돌려주는 async 콜백(HIGH1).
        # 슈퍼바이저 재시작 시 최초 토큰·lease identity 를 재사용하지 않게 한다. 주입되지
        # 않으면(단위 테스트) 최초 params.token 을 그대로 쓴다. 슈퍼바이저가 채널 단위로
        # 이 속성을 주입한다.
        self.token_provider = token_provider
        # 최초 접속 준비 신호(MED8) — 개설 엔드포인트가 이 이벤트로 실제 접속 성공/타임아웃을
        # 판정한다. 슈퍼바이저가 채널 공용 이벤트를 주입한다(없으면 자체 이벤트).
        self.ready: asyncio.Event = asyncio.Event()

        self._room: rtc.Room | None = None
        self._source: rtc.AudioSource | None = None
        self._session: TranslateSession | None = None
        self._resampler: rtc.AudioResampler | None = None
        # 취소·정리 대상 추적(MED6): Floor pump 태스크·AudioStream 을 모아 run() finally 에서
        # 함께 수거해 소켓·태스크 누수를 막는다.
        self._pump_tasks: list[asyncio.Task] = []
        self._streams: list = []

        # 헬스 지표(슈퍼바이저·status 엔드포인트가 읽는다).
        self.seq: int = 0  # 발행한 번역 오디오 프레임 수(순서·진행 확인용)
        self.started_at: float | None = None
        self.session_started_at: float | None = None
        self.last_audio_at: float | None = None

    # ---- 헬스 ----
    @property
    def session_age_seconds(self) -> float | None:
        if self.session_started_at is None:
            return None
        return time.time() - self.session_started_at

    @property
    def renewal_due(self) -> bool:
        """세션이 60분 한계 대비 T-5분 갱신 시점을 지났는지(make-before-break 예정 지점).

        현재는 판정만 노출하고 실제 make-before-break 재접속은 후속 구현이다(구조만 마련).
        """
        age = self.session_age_seconds
        return age is not None and age >= AI_SESSION_RENEW_SECONDS

    # ---- 실행 ----
    async def run(self, stop: asyncio.Event) -> None:
        """워커 1회 실행. stop 세트 또는 정상 종료 시 반환, I/O 오류는 전파(슈퍼바이저 재시작).

        예외를 삼키지 않는다 — 슈퍼바이저가 잡아 백오프 후 새 워커로 재시작한다.
        finally 에서 세션·룸을 반드시 정리해 소켓·participant 누수를 막는다.
        """
        self.started_at = time.time()
        floor_frames: asyncio.Queue = asyncio.Queue(maxsize=_FLOOR_QUEUE_MAX)
        self._pump_tasks = []
        self._streams = []
        tasks: list[asyncio.Task] = []
        try:
            await self._connect_livekit(floor_frames)
            self._session = self._session_factory()
            await self._session.connect(self._params.target_language)
            self.session_started_at = time.time()
            # LiveKit·OpenAI WS 접속 및 session.update 전송까지 성공 — 준비 완료를 알린다(MED8).
            self.ready.set()

            send_task = asyncio.create_task(self._send_loop(floor_frames))
            recv_task = asyncio.create_task(self._recv_loop())
            stop_task = asyncio.create_task(stop.wait())
            tasks = [send_task, recv_task, stop_task]
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            # 루프가 예외로 끝났으면 여기서 재발생시켜 슈퍼바이저가 재시작하게 한다.
            for t in done:
                if t is not stop_task:
                    t.result()
        finally:
            # 메인 루프 태스크 + track_subscribed 에서 생성된 Floor pump 태스크를 모두
            # 취소·수거하고(MED6), AudioStream 을 닫은 뒤 세션·룸을 정리한다.
            pending = tasks + self._pump_tasks
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._aclose_streams()
            await self.aclose()

    async def _aclose_streams(self) -> None:
        """구독한 Floor AudioStream 을 닫는다(네이티브 자원 회수 — best-effort)."""
        for stream in self._streams:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001 — 종료는 best-effort
                pass
        self._streams = []

    async def _resolve_token(self) -> str:
        """이번 (재)접속에 쓸 publish 토큰을 정한다(HIGH1).

        token_provider 가 주입돼 있으면 매 접속마다 fresh 토큰(+lease 재획득)을 받는다 —
        최초 1시간 JWT·lease identity 재사용으로 1시간 뒤 재접속이 영구 거절되던 결함 제거.
        provider 가 없으면(단위 테스트) 최초 params.token 을 그대로 쓴다.
        """
        if self.token_provider is not None:
            return await self.token_provider()
        return self._params.token

    async def _connect_livekit(self, floor_frames: asyncio.Queue) -> None:
        room = self._room_factory()
        self._room = room
        # 출력(번역) 오디오 소스는 OpenAI 출력과 동일한 24kHz mono 로 만들어 리샘플 없이 발행.
        self._source = rtc.AudioSource(AI_INPUT_SAMPLE_RATE, NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track(self._params.publish_track, self._source)

        @room.on("track_subscribed")
        def _on_sub(track_, publication, _participant) -> None:
            if track_.kind != rtc.TrackKind.KIND_AUDIO:
                return
            # Floor(ch-00)만 구독 대상(자기 자신·타 채널 트랙 무시).
            if publication.name != self._params.subscribe_track:
                return
            stream = rtc.AudioStream(
                track_,
                sample_rate=AI_FLOOR_SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                frame_size_ms=20,
            )
            # 스트림·pump 태스크를 추적해 run() finally 에서 함께 수거한다(MED6).
            self._streams.append(stream)
            self._pump_tasks.append(asyncio.create_task(self._pump_floor(stream, floor_frames)))

        token = await self._resolve_token()
        await room.connect(
            self._params.rtc_url,
            token,
            rtc.RoomOptions(auto_subscribe=True, dynacast=False),
        )
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, opts)

    async def _pump_floor(self, stream: "rtc.AudioStream", floor_frames: asyncio.Queue) -> None:
        """Floor AudioStream 프레임을 큐로 흘려보낸다(큐가 차면 오래된 프레임 폐기·논블록)."""
        async for event in stream:
            frame = event.frame
            if floor_frames.full():
                with_suppress_get(floor_frames)
            floor_frames.put_nowait(frame)

    async def _send_loop(self, floor_frames: asyncio.Queue) -> None:
        """Floor 프레임을 24kHz 로 리샘플해 OpenAI 로 append 한다(무음 포함 연속 전송)."""
        assert self._session is not None
        while True:
            frame = await floor_frames.get()
            for pcm24k in self._resample_to_24k(frame):
                await self._session.append_audio(pcm24k)

    def _resample_to_24k(self, frame: "rtc.AudioFrame") -> list[bytes]:
        """Floor 48kHz 프레임을 OpenAI 입력용 24kHz mono PCM16 바이트로 변환한다.

        입력이 이미 24kHz 면 리샘플 없이 그대로 반환한다. AudioResampler.push 는
        내부 버퍼링으로 0개 이상의 프레임을 반환하므로 리스트로 모아 넘긴다.
        """
        if frame.sample_rate == AI_INPUT_SAMPLE_RATE:
            return [frame.data.tobytes()]
        if self._resampler is None:
            self._resampler = rtc.AudioResampler(
                frame.sample_rate, AI_INPUT_SAMPLE_RATE, num_channels=NUM_CHANNELS
            )
        return [f.data.tobytes() for f in self._resampler.push(frame)]

    async def _recv_loop(self) -> None:
        """OpenAI 수신 이벤트를 처리한다(번역 오디오 발행 + 자막 콜백).

        session.closed 이벤트를 만나면 이터레이터를 더 소비하지 않고 즉시 종료한다(MED5).
        error 이벤트는 _handle_event 가 예외로 전환해 여기서 전파된다(슈퍼바이저 재접속).
        """
        assert self._session is not None
        async for event in self._session.events():
            if not await self._handle_event(event):
                break

    async def _handle_event(self, event: dict) -> bool:
        """번역 세션 이벤트 1건 처리(테스트 진입점). 반환: recv 루프를 계속할지 여부.

        - session.output_audio.delta: 번역 음성 → AudioSource.capture_frame(ch-NN 발행).
        - session.output_transcript.delta: 목표 언어 자막 → on_transcript 콜백.
        - session.input_transcript.delta: 원문 자막 → on_transcript 콜백(source 표시).
        - error: 인증·설정·서버 오류 → 로그 후 예외로 전환(슈퍼바이저가 백오프 후 재접속).
        - session.closed: 세션 종료 → recv 루프를 끝낸다(False 반환, 슈퍼바이저 재시작).
        """
        etype = event.get("type")
        if etype == "session.output_audio.delta":
            await self._emit_audio(event.get("delta", ""))
        elif etype == "session.output_transcript.delta":
            if self._on_transcript is not None:
                self._on_transcript("target", event.get("delta", ""))
        elif etype == "session.input_transcript.delta":
            if self._on_transcript is not None:
                self._on_transcript("source", event.get("delta", ""))
        elif etype == "error":
            err = event.get("error") or {}
            log.warning("ai_worker_openai_error ch=%s err=%s", self._params.channel_id, err)
            raise TranslateSessionError(f"openai_error: {err}")
        elif etype == "session.closed":
            log.info("ai_worker_session_closed ch=%s", self._params.channel_id)
            return False
        return True

    async def _emit_audio(self, delta_b64: str) -> None:
        """번역 오디오 델타를 LiveKit ch-NN 트랙으로 발행한다."""
        if not delta_b64 or self._source is None:
            return
        pcm = decode_output_delta(delta_b64)
        samples = output_frame_samples(pcm)
        if samples == 0:
            return
        frame = rtc.AudioFrame(pcm, AI_INPUT_SAMPLE_RATE, NUM_CHANNELS, samples)
        await self._source.capture_frame(frame)
        self.seq += 1
        self.last_audio_at = time.time()

    async def aclose(self) -> None:
        """세션·룸을 정리한다(server-first: OpenAI 세션 먼저 닫고 룸 disconnect)."""
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception:  # noqa: BLE001 — 종료는 best-effort
                log.exception("ai_worker_room_disconnect_failed")
            self._room = None


def with_suppress_get(q: asyncio.Queue) -> None:
    """큐에서 한 항목을 논블록으로 버린다(백프레셔 — 가장 오래된 프레임 폐기)."""
    try:
        q.get_nowait()
    except asyncio.QueueEmpty:
        pass


# ---- 채널 슈퍼바이저 ----
class AiTranslateChannel:
    """AI 통역 채널 1개(목표 언어 1개)의 워커 수명주기를 관리하는 슈퍼바이저.

    워커 크래시 시 지수 백오프로 재시작한다(LiveKit 룸은 empty_timeout 24h 로 생존 →
    자동 재구독). stop 은 server-first(현재 워커 aclose → 룸 disconnect)로 종료하고,
    finally 로 감독 task 를 취소해 고착을 막는다(PhoneHostServer.stop 교훈).
    """

    def __init__(
        self,
        channel_id: int,
        target_language: str,
        worker_factory: Callable[[], TranslateWorker],
        backoff_base: float = AI_WORKER_BACKOFF_BASE_SECONDS,
        max_backoff: float = AI_WORKER_BACKOFF_MAX_SECONDS,
        token_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self.channel_id = channel_id
        self.target_language = target_language
        self._worker_factory = worker_factory
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._token_provider = token_provider

        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._worker: TranslateWorker | None = None
        # 최초 접속 준비 신호(MED8) — 재시작해도 유지되는 채널 공용 이벤트. 워커마다 주입해
        # 어느 워커든 최초 접속에 성공하면 개설 엔드포인트가 준비 완료로 판정한다.
        self.ready: asyncio.Event = asyncio.Event()
        self.started_at = time.time()
        self.restart_count = 0
        self.last_error: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        backoff = self._backoff_base
        first = True
        while not self._stop.is_set():
            worker = self._worker_factory()
            self._worker = worker
            # 첫 연결은 생성자에 주입된 최초 토큰을 그대로 쓴다 — 개설 응답·감사 로그에 기록한
            # identity 가 첫 접속 lease 와 일치하게(회귀4). 재접속(2번째 이후)에만 fresh 토큰
            # 프로바이더를 호출해 새 JWT·lease 를 받는다(HIGH1). 준비 이벤트는 채널 공용(MED8).
            worker.token_provider = None if first else self._token_provider
            worker.ready = self.ready
            try:
                await worker.run(self._stop)
                backoff = self._backoff_base  # 정상 실행 후 백오프 초기화.
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 모든 워커 오류를 재시작으로 흡수
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.restart_count += 1
                log.warning(
                    "ai_worker_crash ch=%s restart=%s err=%s",
                    self.channel_id,
                    self.restart_count,
                    self.last_error,
                )
            first = False  # 다음 루프부터는 재접속 — fresh 토큰 프로바이더 사용.
            if self._stop.is_set():
                break
            # 재시작 전 백오프(stop 이 오면 즉시 중단). backoff_base=0 이면 즉시 재시도.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            # 다음 재시작 백오프를 2배로(상한까지). base=0 이면 0 유지(즉시 재시도).
            backoff = min(self._max_backoff, backoff * 2)

    async def stop(self, timeout: float | None = None) -> bool:
        """정지를 요청하고 감독 task 가 실제로 끝났는지 반환한다(회귀3).

        server-first: stop 신호 → 감독 task **강제 취소**(워커 정리가 고착돼도 반드시
        풀리도록) → 현재 워커 aclose best-effort → 감독 task 종료 대기. task 의 run() finally
        도 세션·룸을 정리하지만, fake 워커·즉시 종료 대비로 aclose 를 한 번 더 트리거한다.
        timeout 안에 task 가 끝나면 True, 아니면 False(호출부가 leaked 로 추적·후속 수거).
        """
        self._stop.set()
        task = self._task
        worker = self._worker
        # 감독 task 를 먼저 강제 취소한다 — worker.aclose/room.disconnect 고착에 종료가
        # 물리지 않게(먼저 pop 하고 나중에 취소하던 순서 문제를 제거).
        if task is not None:
            task.cancel()
        if worker is not None:
            try:
                if timeout is None:
                    await worker.aclose()
                else:
                    await asyncio.wait_for(worker.aclose(), timeout)
            except Exception:  # noqa: BLE001 — 타임아웃 포함 종료는 best-effort
                log.warning("ai_worker_aclose_failed ch=%s", self.channel_id)
        if task is None:
            return True
        try:
            if timeout is None:
                await task
            else:
                # shield 로 감싸 wait_for 가 (이미 취소된) task 를 재취소·재대기하며 고착되지
                # 않게 하고, 제한시간 초과 시 즉시 빠져나온다(task 는 계속 살아 있음).
                await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        return task.done()

    def status(self) -> dict:
        worker = self._worker
        running = self._task is not None and not self._task.done()

        def _iso(ts: float | None) -> str | None:
            if ts is None:
                return None
            import datetime as _dt

            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()

        return {
            "channel_id": self.channel_id,
            "target_language": self.target_language,
            "running": running,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "started_at": _iso(self.started_at),
            "session_age_seconds": worker.session_age_seconds if worker else None,
            "last_audio_at": _iso(worker.last_audio_at) if worker else None,
            "seq": worker.seq if worker else 0,
            "renewal_due": worker.renewal_due if worker else False,
        }


class AiChannelManager:
    """열려 있는 AI 통역 채널들의 슈퍼바이저 레지스트리(채널 id → AiTranslateChannel).

    worker_factory 는 AiWorkerParams → TranslateWorker 를 만드는 주입 지점이다.
    운영 기본값은 실제 OpenAI WS + rtc.Room 워커를, 테스트는 fake 워커를 주입한다.
    """

    def __init__(
        self,
        worker_factory: Callable[[AiWorkerParams], TranslateWorker],
        backoff_base: float = AI_WORKER_BACKOFF_BASE_SECONDS,
        max_backoff: float = AI_WORKER_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._worker_factory = worker_factory
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._channels: dict[int, AiTranslateChannel] = {}
        # stop 이 제한시간 안에 감독 task 를 끝내지 못한 채널(정리 고착)을 담아 참조를 잃지
        # 않게 한다 — lifespan 의 close() 가 마지막에 강제 수거한다(회귀3).
        self._leaked: set[AiTranslateChannel] = set()

    def start(
        self,
        params: AiWorkerParams,
        token_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> AiTranslateChannel:
        channel = AiTranslateChannel(
            params.channel_id,
            params.target_language,
            worker_factory=lambda: self._worker_factory(params),
            backoff_base=self._backoff_base,
            max_backoff=self._max_backoff,
            token_provider=token_provider,
        )
        self._channels[params.channel_id] = channel
        channel.start()
        return channel

    def get(self, channel_id: int) -> "AiTranslateChannel | None":
        """이 슬롯에 현재 등록된 슈퍼바이저 채널을 반환한다(없으면 None).

        롤백·정리 경로가 '내가 시작한 바로 그 채널'인지 객체 동일성으로 판정하는 데 쓴다.
        """
        return self._channels.get(channel_id)

    async def stop(self, channel_id: int, timeout: float | None = None) -> bool:
        """채널 정지. 감독 task 가 실제로 끝난 것을 확인한 뒤에만 활성 레지스트리에서 뺀다.

        timeout 안에 task 가 끝나지 않으면(정리 고착) 채널을 활성 레지스트리에서 빼되 참조를
        leaked 에 보관해, 매니저 상태와 실제 task 생존이 어긋나지 않게 한다(회귀3). leaked 는
        close() 가 마지막에 강제 수거한다.
        """
        channel = self._channels.get(channel_id)
        if channel is None:
            return False
        stopped = await channel.stop(timeout=timeout)
        self._channels.pop(channel_id, None)
        if stopped:
            self._leaked.discard(channel)
        else:
            self._leaked.add(channel)
            log.warning("ai_channel_stop_incomplete ch=%s", channel_id)
        return True

    def status(self, channel_id: int) -> dict | None:
        channel = self._channels.get(channel_id)
        return channel.status() if channel is not None else None

    def has(self, channel_id: int) -> bool:
        return channel_id in self._channels

    async def close(self) -> None:
        """모든 AI 채널을 병렬 정지한다(MED7·회귀3).

        stop 1건당 타임아웃을 둬 한 워커의 aclose/room.disconnect 지연이 전체 종료(그리고
        lifespan 의 녹음·DB 정리)를 무기한 막지 않게 한다. 타임아웃으로 못 멈춘 감독 task 는
        leaked 로 추적되며, 마지막에 강제 취소·수거한다(참조를 잃지 않는다).
        """
        await asyncio.gather(
            *(
                self.stop(cid, timeout=AI_WORKER_STOP_TIMEOUT_SECONDS)
                for cid in list(self._channels)
            ),
            return_exceptions=True,
        )
        await self._reap_leaked()

    async def _reap_leaked(self) -> None:
        """stop 타임아웃으로 못 멈춘 감독 task 를 강제 취소·수거한다(회귀3, best-effort)."""
        channels = list(self._leaked)
        self._leaked.clear()
        tasks = [c._task for c in channels if c._task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=AI_WORKER_STOP_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001 — 마지막 수거도 무기한 대기하지 않는다
                log.warning("ai_leaked_task_reap_timeout count=%s", len(tasks))


def build_default_ai_worker_factory(
    settings,
) -> Callable[[AiWorkerParams], TranslateWorker]:
    """운영 기본 워커 팩토리 — 실제 OpenAI WS 세션 + rtc.Room 을 쓰는 워커를 만든다."""

    def factory(params: AiWorkerParams) -> TranslateWorker:
        def session_factory() -> TranslateSession:
            return RealtimeTranslateSession(settings.openai_api_key)

        return TranslateWorker(params, session_factory=session_factory)

    return factory
