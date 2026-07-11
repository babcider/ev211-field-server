# LiveKit 원격 오디오를 서버에서 채널별로 혼합해 MP3 파일로 저장하는 녹음 관리자
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from livekit import rtc

from .tokens import issue_recording_token

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
NUM_CHANNELS = 1


class RecordingError(RuntimeError):
    """녹음을 시작하거나 종료할 수 없을 때 반환하는 운영 오류."""


@dataclass
class RecordingInfo:
    recording_id: str
    kind: str
    channel_id: int
    label: str
    room: str
    started_at: str
    ended_at: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None

    def public(self, active: bool) -> dict:
        return {
            "recording_id": self.recording_id,
            "kind": self.kind,
            "channel_id": self.channel_id,
            "label": self.label,
            "room": self.room,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "active": active,
        }


async def _audio_frames(stream: rtc.AudioStream) -> AsyncIterator[rtc.AudioFrame]:
    """AudioStream의 이벤트 래퍼를 AudioMixer가 받는 AudioFrame으로 변환한다."""
    async for event in stream:
        yield event.frame


class _RecordingSession:
    def __init__(
        self,
        info: RecordingInfo,
        directory: Path,
        rtc_url: str,
        api_key: str,
        api_secret: str,
        track_name: str | None,
        on_disconnected: Callable[[str], Awaitable[None]],
    ) -> None:
        self.info = info
        self._directory = directory
        self._rtc_url = rtc_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._track_name = track_name
        self._on_disconnected = on_disconnected
        self._room = rtc.Room()
        self._mixer: rtc.AudioMixer | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._writer_task: asyncio.Task | None = None
        self._streams: dict[str, tuple[rtc.AudioStream, AsyncIterator[rtc.AudioFrame]]] = {}
        self._stopping = False

    @property
    def _part_path(self) -> Path:
        return self._directory / f"{self.info.recording_id}.part.mp3"

    @property
    def output_path(self) -> Path:
        return self._directory / f"{self.info.recording_id}.mp3"

    async def start(self) -> None:
        lame = shutil.which("lame")
        if lame is None:
            raise RecordingError("lame MP3 인코더를 찾을 수 없습니다.")

        self._directory.mkdir(parents=True, exist_ok=True)
        self._mixer = rtc.AudioMixer(
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            blocksize=4_800,
            stream_timeout_ms=150,
            capacity=50,
        )
        self._process = await asyncio.create_subprocess_exec(
            lame,
            "--silent",
            "-r",
            "-s",
            "48",
            "--bitwidth",
            "16",
            "--signed",
            "--little-endian",
            "-m",
            "m",
            "-b",
            "128",
            "-",
            str(self._part_path),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._writer_task = asyncio.create_task(self._write_mixed_audio())

        @self._room.on("track_subscribed")
        def _track_subscribed(track, publication, _participant) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if self._track_name is not None and publication.name != self._track_name:
                return
            stream = rtc.AudioStream(
                track,
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                frame_size_ms=20,
                capacity=100,
            )
            frames = _audio_frames(stream)
            self._streams[track.sid] = (stream, frames)
            if self._mixer is not None:
                self._mixer.add_stream(frames)

        @self._room.on("track_unsubscribed")
        def _track_unsubscribed(track, *_args) -> None:
            asyncio.create_task(self._remove_track(track.sid))

        @self._room.on("disconnected")
        def _disconnected(*_args) -> None:
            if not self._stopping:
                asyncio.create_task(self._on_disconnected(self.info.recording_id))

        token, _identity = issue_recording_token(
            self._api_key, self._api_secret, self.info.room
        )
        try:
            await self._room.connect(
                self._rtc_url,
                token,
                rtc.RoomOptions(auto_subscribe=True, dynacast=False),
            )
        except Exception as exc:
            await self.stop(finalize=False)
            raise RecordingError(f"LiveKit 녹음 연결에 실패했습니다. {exc}") from exc

    async def _remove_track(self, sid: str) -> None:
        pair = self._streams.pop(sid, None)
        if pair is None:
            return
        stream, frames = pair
        if self._mixer is not None:
            self._mixer.remove_stream(frames)
        await stream.aclose()

    async def _write_mixed_audio(self) -> None:
        assert self._mixer is not None
        assert self._process is not None and self._process.stdin is not None
        async for frame in self._mixer:
            self._process.stdin.write(frame.data.tobytes())
            await self._process.stdin.drain()

    async def stop(self, finalize: bool = True) -> None:
        if self._stopping:
            return
        self._stopping = True

        try:
            await self._room.disconnect()
        except Exception:
            log.exception("녹음 participant 연결 종료 실패")

        for sid in list(self._streams):
            await self._remove_track(sid)

        if self._mixer is not None:
            self._mixer.end_input()

        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self._writer_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer_task.cancel()
                if self._mixer is not None:
                    await self._mixer.aclose()

        process = self._process
        if process is not None and process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        exit_code = None
        stderr = b""
        if process is not None:
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=10)
                if process.stderr is not None:
                    stderr = await process.stderr.read()
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                exit_code = -1

        if not finalize:
            self._part_path.unlink(missing_ok=True)
            return
        if exit_code != 0 or not self._part_path.exists():
            self._part_path.unlink(missing_ok=True)
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RecordingError(f"LAME MP3 인코딩에 실패했습니다. {detail}".strip())
        self._part_path.replace(self.output_path)


class RecordingManager:
    """채널별 녹음 생명주기와 MP3 메타데이터를 관리한다."""

    def __init__(self, directory: str, rtc_url: str, api_key: str, api_secret: str) -> None:
        self._directory = Path(directory)
        self._rtc_url = rtc_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._active: dict[str, _RecordingSession] = {}
        self._channel_index: dict[tuple[str, int], str] = {}
        self._lock = asyncio.Lock()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_files()

    def _recover_interrupted_files(self) -> None:
        for path in self._directory.glob("*.part.mp3"):
            # 비정상 종료 파일은 완결성을 보장할 수 없으므로 공개 목록에 올리지 않는다.
            path.unlink(missing_ok=True)

    async def start(
        self,
        kind: str,
        channel_id: int,
        label: str,
        room: str,
        track_name: str | None = None,
    ) -> dict:
        key = (kind, channel_id)
        async with self._lock:
            existing = self._channel_index.get(key)
            if existing is not None:
                raise RecordingError("이 채널은 이미 녹음 중입니다.")
            recording_id = str(uuid.uuid4())
            info = RecordingInfo(
                recording_id=recording_id,
                kind=kind,
                channel_id=channel_id,
                label=label,
                room=room,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            session = _RecordingSession(
                info,
                self._directory,
                self._rtc_url,
                self._api_key,
                self._api_secret,
                track_name,
                self._stop_after_disconnect,
            )
            self._active[recording_id] = session
            self._channel_index[key] = recording_id

        try:
            await session.start()
        except Exception:
            async with self._lock:
                self._active.pop(recording_id, None)
                self._channel_index.pop(key, None)
            raise
        return info.public(active=True)

    async def _stop_after_disconnect(self, recording_id: str) -> None:
        try:
            await self.stop(recording_id)
        except Exception:
            log.exception("LiveKit 연결 종료 후 녹음 마감 실패")

    async def stop(self, recording_id: str) -> dict:
        async with self._lock:
            session = self._active.pop(recording_id, None)
            if session is None:
                raise RecordingError("진행 중인 녹음을 찾을 수 없습니다.")
            self._channel_index.pop((session.info.kind, session.info.channel_id), None)

        await session.stop(finalize=True)
        ended = datetime.now(timezone.utc)
        started = datetime.fromisoformat(session.info.started_at)
        session.info.ended_at = ended.isoformat()
        session.info.duration_seconds = round((ended - started).total_seconds(), 1)
        session.info.size_bytes = session.output_path.stat().st_size
        self._write_metadata(session.info)
        return session.info.public(active=False)

    def _write_metadata(self, info: RecordingInfo) -> None:
        final = self._directory / f"{info.recording_id}.json"
        temp = self._directory / f"{info.recording_id}.json.tmp"
        temp.write_text(json.dumps(info.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(final)

    def list(self) -> dict:
        active = [s.info.public(active=True) for s in self._active.values()]
        completed = []
        for path in self._directory.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                info = RecordingInfo(**raw)
                mp3 = self._directory / f"{info.recording_id}.mp3"
                if mp3.is_file():
                    info.size_bytes = mp3.stat().st_size
                    completed.append(info.public(active=False))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                log.warning("잘못된 녹음 메타데이터를 건너뜁니다. %s", path.name)
        active.sort(key=lambda x: x["started_at"], reverse=True)
        completed.sort(key=lambda x: x["started_at"], reverse=True)
        return {"active": active, "recordings": completed}

    def download_path(self, recording_id: str) -> Path | None:
        try:
            uuid.UUID(recording_id)
        except ValueError:
            return None
        path = self._directory / f"{recording_id}.mp3"
        if not path.is_file() or path.parent.resolve() != self._directory.resolve():
            return None
        return path

    async def close(self) -> None:
        for recording_id in list(self._active):
            try:
                await self.stop(recording_id)
            except Exception:
                log.exception("서버 종료 중 녹음 마감 실패")
