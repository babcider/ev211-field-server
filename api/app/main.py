# FastAPI 앱 — 채널·토큰·상태·웹훅·admin 엔드포인트와 기동 시퀀스를 정의한다
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Body, FastAPI, Header, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .config import (
    FLOOR_CHANNEL_ID,
    INTERCOM_MAX_CHANNELS,
    INTERCOM_MAX_PARTICIPANTS,
    LEASE_JOIN_GRACE_SECONDS,
    LISTENER_HEARTBEAT_TTL_SECONDS,
    PUBLISH_TTL_SECONDS,
    SIGNAL_LOG_CLEANUP_INTERVAL_SECONDS,
    SIGNAL_LOG_RETENTION_SECONDS,
    load_settings,
)
from .db import ChannelExists, Database, new_nonce
from .http_util import client_ip, is_https
from .livekit_client import LiveKitClient
from .recording import RecordingError
from .state import AppState
from .tokens import (
    intercom_response,
    issue_intercom_token,
    issue_monitor_token,
    issue_publish_token,
    issue_subscribe_token,
    monitor_response,
    publish_response,
    subscribe_response,
)
from .webhook import WebhookProcessor

_BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
log = logging.getLogger(__name__)

# 초기(배포 기본) 관리자 비밀번호. 이 값인 동안 서버는 "관리자 비밀번호 변경·상태 조회"
# 외의 관리자 작업을 거부한다(13차 #3 — 앱 UI 우회 차단). 새 비밀번호로도 금지된다.
INITIAL_ADMIN_PASSWORD = "0000"


# ---- 요청 모델 ----
class ChannelCreateBody(BaseModel):
    language: str
    label: str = Field(min_length=1, max_length=40)
    channel_id: int | None = Field(default=None, ge=0, le=15)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        if not _BCP47.match(v):
            raise ValueError("언어 코드는 BCP-47 형식이어야 합니다.")
        return v


class PublishTokenBody(BaseModel):
    channel_id: int = Field(ge=0, le=15)


def _clean_display(v: str) -> str:
    # 제어 문자를 제거해 표시 전용 문자열로 정규화한다(JWT name claim·채널명에 실림).
    return "".join(ch for ch in v.strip() if ch.isprintable())


class IntercomTokenBody(BaseModel):
    """인터컴 토큰 발급 요청. name 은 참가자 목록에 보이는 별명(선택)."""

    name: str = Field(default="", max_length=20)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_display(v)


class IntercomChannelCreateBody(BaseModel):
    """무전기 채널 개설 요청. channel_name 은 필수(생성자가 정함), password 는 선택."""

    channel_name: str = Field(min_length=1, max_length=30)
    password: str | None = Field(default=None, max_length=64)

    @field_validator("channel_name")
    @classmethod
    def _cname(cls, v: str) -> str:
        cleaned = _clean_display(v)
        if not cleaned:
            raise ValueError("채널 이름을 입력하세요.")
        return cleaned

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v == "":
            return None  # 빈 문자열은 비밀번호 없음으로 취급.
        if len(v) < 4:
            raise ValueError("채널 비밀번호는 4자 이상이어야 합니다.")
        if not v.isascii():
            raise ValueError("채널 비밀번호는 영문·숫자·기호(ASCII)만 사용할 수 있습니다.")
        return v


class IntercomChannelEnterBody(BaseModel):
    """무전기 채널 입장 요청. name 은 표시 별명(선택), password 는 채널 비번(있으면 필수)."""

    name: str = Field(default="", max_length=20)
    password: str = Field(default="", max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_display(v)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class HeartbeatBody(BaseModel):
    channel_id: int = Field(ge=0, le=15)
    listener_id: str = Field(min_length=36, max_length=36)

    @field_validator("listener_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        if not _UUID_RE.match(v):
            raise ValueError("listener_id 는 UUID 형식이어야 합니다.")
        return v


# 변경 금지 비밀번호(자리표시자·흔한 기본값·초기 관리자 비밀번호). "0000" 은 초기
# 배포 기본값이므로 새 비밀번호로 되돌아가는 것을 막는다(12차 #3 — 알려진 자격증명).
_FORBIDDEN_PASSWORDS = {"change-me", "changeme", "password", "default", "0000"}


class PasswordChangeBody(BaseModel):
    new_password: str = Field(min_length=4, max_length=64)

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 4:
            raise ValueError("비밀번호는 4자 이상이어야 합니다.")
        # 비ASCII 는 상수시간 비교·헤더 인코딩 문제를 일으키므로 제한한다(12차 #4).
        if not v.isascii():
            raise ValueError("비밀번호는 영문·숫자·기호(ASCII)만 사용할 수 있습니다.")
        if v.lower() in _FORBIDDEN_PASSWORDS:
            raise ValueError("사용할 수 없는 비밀번호입니다.")
        return v


class RecordingStartBody(BaseModel):
    """관리자가 시작할 녹음 대상. relay는 방송 채널, intercom은 무전기 채널이다."""

    kind: Literal["relay", "intercom"]
    channel_id: int = Field(ge=0, le=15)


async def _signal_log_cleanup_loop(state: AppState) -> None:
    """하루마다 30일 보존 기한을 지난 신호 이벤트를 정리한다."""
    while True:
        await asyncio.sleep(SIGNAL_LOG_CLEANUP_INTERVAL_SECONDS)
        try:
            deleted = state.db.purge_signal_events(SIGNAL_LOG_RETENTION_SECONDS)
            if deleted:
                log.info("purged_signal_events count=%s", deleted)
        except Exception:
            # 일시 DB 오류로 정리 task가 영구 종료되지 않게 다음 주기에 재시도한다.
            log.exception("signal_event_cleanup_failed")


# ---- 앱 팩토리 ----
def create_app(state: AppState | None = None) -> FastAPI:
    """앱을 생성한다. state 를 주입하면(테스트) 기동 시퀀스를 건너뛴다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_db = False
        if getattr(app.state, "field", None) is None:
            settings = load_settings()
            db = Database(settings.db_path)
            livekit = LiveKitClient(
                settings.livekit_host, settings.livekit_api_key, settings.livekit_api_secret
            )
            st = AppState(settings, db, livekit)
            await st.bootstrap()
            app.state.field = st
            owns_db = True
        app.state.webhook = WebhookProcessor(app.state.field)
        deleted = app.state.field.db.purge_signal_events(SIGNAL_LOG_RETENTION_SECONDS)
        if deleted:
            log.info("purged_signal_events_on_startup count=%s", deleted)
        cleanup_task = asyncio.create_task(_signal_log_cleanup_loop(app.state.field))
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            await app.state.field.recordings.close()
            # lifespan 이 생성한 DB 연결만 닫는다(테스트 주입 state 는 픽스처가 관리).
            if owns_db:
                app.state.field.db.close()

    app = FastAPI(title="EV211 field-api", version="0.1.0", lifespan=lifespan)

    if state is not None:
        app.state.field = state
        app.state.webhook = WebhookProcessor(state)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        # 422 검증 오류를 공통 Error 스키마({code, message})로 변환한다.
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "요청 본문 검증에 실패했습니다.",
                "details": {"errors": _safe_errors(exc)},
            },
        )

    _register_routes(app)
    return app


def _safe_errors(exc: RequestValidationError) -> list[dict]:
    """검증 오류를 JSON 직렬화 가능한 형태로 축약한다(민감 값·비직렬 객체 제거)."""
    out = []
    for e in exc.errors():
        out.append({"loc": [str(x) for x in e.get("loc", [])], "msg": str(e.get("msg", ""))})
    return out


def _st(request: Request) -> AppState:
    return request.app.state.field


def _err(code: str, message: str, status: int, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=status, content={"code": code, "message": message}, headers=headers)


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


def _ip(request: Request, st: AppState) -> str:
    """신뢰 프록시(Caddy) 기반으로 실제 클라이언트 IP 를 산출한다."""
    return client_ip(request, st.settings.forwarded_allow_ips)


def _is_loopback(ip: str) -> bool:
    """실제 클라이언트 IP 가 루프백(127.0.0.0/8 · ::1)인지 판정한다(#4)."""
    import ipaddress

    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _https_guard(request: Request, st: AppState) -> JSONResponse | None:
    """인증(비밀번호) 경로 전용 — http 로 온 요청이면 403 을 반환한다(평문 비번 전송 차단).

    #4 루프백 예외: 실제 클라이언트 IP 가 127.0.0.1/::1(서버 자신, Caddy 경유 XFF 포함)
    이면 http 인증 요청을 허용한다. 이는 브라우저 getUserMedia 가 localhost 를 안전
    출처로 취급하는 것과 동일한 논리이며, 평문이라도 트래픽이 노드 밖으로 나가지 않는다.
    외부(비루프백) IP 의 http 인증 요청은 그대로 403 으로 거부한다.
    """
    if is_https(request, st.settings.forwarded_allow_ips):
        return None
    if _is_loopback(_ip(request, st)):
        return None
    return _err(
        "https_required",
        "이 요청은 HTTPS(wss/https)로만 허용됩니다. 자체서명 https(:8443)로 접속하세요.",
        403,
    )


def _auth_bearer(authorization: str | None, expected: str) -> bool:
    """비밀번호를 상수시간 비교로 검증한다(타이밍 공격 방어).

    compare_digest 는 비ASCII str 에서 TypeError 를 던지므로 UTF-8 bytes 로 비교한다
    (12차 #4 — 비ASCII 비밀번호가 저장돼 있어도 인증이 500 으로 깨지지 않게).
    """
    import secrets as _secrets

    token = _bearer(authorization)
    if token is None:
        return False
    return _secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def _intercom_user_count(participants: list) -> int:
    """관리자 모니터·서버 녹음을 제외한 실제 무전기 사용자 수를 센다."""
    return sum(1 for p in participants if str(getattr(p, "identity", "")).startswith("intercom-"))


# scrypt 파라미터(대화형 로그인 수준). 저장 형식: "scrypt$<salt_hex>$<dk_hex>".
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _channel_password_hash(password: str) -> str:
    """무전기 채널 비밀번호를 채널별 랜덤 salt + scrypt 로 해시한다(22차).

    무염 SHA-256 은 DB 유출 시 짧은 비번을 즉시 사전 대입당하므로, salt 를 붙이고
    메모리-하드 KDF(scrypt)로 오프라인 대입 비용을 높인다. 채널은 임시라 장기
    크리덴셜은 아니지만, 유출 시 즉시 역산되지 않도록 최소 방어를 둔다.
    """
    import hashlib
    import os

    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def _channel_password_matches(password: str, stored: str) -> bool:
    """저장 해시(scrypt$salt$dk)와 입력 비밀번호를 상수시간 비교한다.

    저장 salt 로 동일 파라미터 scrypt 를 재계산해 dk 를 상수시간 비교한다. 형식이
    깨진 저장값은 안전하게 불일치로 처리한다.
    """
    import hashlib
    import secrets as _secrets

    try:
        scheme, salt_hex, dk_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected)
    )
    return _secrets.compare_digest(dk, expected)


def _channel_view(st: AppState, ch) -> dict:
    hb = st.db.count_heartbeats_by_channel()
    approx = st.db.token_approximation_by_channel()
    use_hb = st.db.has_any_live_heartbeat()
    listeners = hb.get(ch.channel_id, 0) if use_hb else approx.get(ch.channel_id, 0)
    return {
        "channel_id": ch.channel_id,
        "track_name": ch.track_name,
        "language": ch.language,
        "label": ch.label,
        "state": ch.state,
        "on_air": st.on_air.is_on_air(ch.channel_id),
        "listeners": listeners,
    }


def _register_routes(app: FastAPI) -> None:  # noqa: C901 — 라우트 집합 등록
    # ---- meta ----
    @app.get("/healthz")
    async def healthz(request: Request):
        st = _st(request)
        connected = await st.livekit.connected()
        return {
            "status": "ok" if connected else "degraded",
            "livekit": "connected" if connected else "disconnected",
            "generation": st.generation,
        }

    # ---- channels ----
    @app.get("/channels")
    async def list_channels(request: Request):
        st = _st(request)
        st.db.purge_expired_heartbeats()
        st.db.purge_expired_issued_listeners()
        channels = [_channel_view(st, c) for c in st.db.list_channels()]
        return {
            "channels": channels,
            "generation": st.generation,
            "max_channels": st.settings.max_channels,
            "listeners_source": "heartbeat" if st.db.has_any_live_heartbeat() else "token_approximation",
        }

    @app.post("/channels", status_code=201)
    async def create_channel(
        request: Request,
        body: ChannelCreateBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.publish_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다. 잠시 후 다시 시도하세요.", 423, retry)
        ok, rretry = st.publish_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.send_password):
            st.publish_lock.record_failure(ip)
            return _err("unauthorized", "인증이 필요합니다.", 401)
        st.publish_lock.record_success(ip)

        # 채널 번호 결정.
        if body.channel_id is not None:
            cid = body.channel_id
        else:
            cid = st.db.lowest_free_general_slot(st.settings.max_channels)
            if cid is None:
                return _err("max_channels_reached", f"최대 채널 수({st.settings.max_channels})에 도달했습니다.", 409)
        # 일반 채널 한도 검사(채널 0 Floor 는 한도 제외).
        if cid != FLOOR_CHANNEL_ID and st.db.count_open_general_channels() >= st.settings.max_channels:
            existing = st.db.get_channel(cid)
            if existing is None or existing.state != "open":
                return _err("max_channels_reached", f"최대 채널 수({st.settings.max_channels})에 도달했습니다.", 409)
        try:
            ch = st.db.create_channel(cid, body.language, body.label)
        except ChannelExists:
            return _err("max_channels_reached", "이미 개설된 채널입니다.", 409)
        return _channel_full(st, ch)

    @app.get("/channels/{channel_id}")
    async def get_channel(request: Request, channel_id: int = Path(ge=0, le=15)):
        st = _st(request)
        ch = st.db.get_channel(channel_id)
        if ch is None:
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)
        return _channel_full(st, ch)

    @app.delete("/channels/{channel_id}", status_code=204)
    async def close_channel(
        request: Request,
        channel_id: int = Path(ge=0, le=15),
        authorization: str | None = Header(default=None),
    ):
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.publish_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다.", 423, retry)
        ok, rretry = st.publish_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        # 송신자 비밀번호 외에 관리자 비밀번호로도 허용한다(관리자 메뉴의 채널 삭제).
        is_send_auth = _auth_bearer(authorization, st.send_password)
        is_admin_auth = _auth_bearer(authorization, st.admin_password)
        if not (is_send_auth or is_admin_auth):
            st.publish_lock.record_failure(ip)
            return _err("unauthorized", "인증이 필요합니다.", 401)
        # 초기 관리자 비밀번호(0000)로는 관리자 권한 작업을 허용하지 않는다(13차 #3).
        if is_admin_auth and not is_send_auth and st.admin_password == INITIAL_ADMIN_PASSWORD:
            return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
        st.publish_lock.record_success(ip)

        ch = st.db.get_channel(channel_id)
        if ch is None or ch.state != "open":
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)
        # 회전 락(14차 #3): 인증 재검증부터 RemoveParticipant·상태 전이까지 자격증명
        # 회전을 배제한다 — 구 비밀번호 요청이 송신자를 끊는 것을 막는다.
        async with st.rotation_lock:
            # 락 획득 후 인증·초기 비밀번호 게이트 재검증(14차 #2).
            is_send_auth = _auth_bearer(authorization, st.send_password)
            is_admin_auth = _auth_bearer(authorization, st.admin_password)
            if not (is_send_auth or is_admin_auth):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            if is_admin_auth and not is_send_auth and st.admin_password == INITIAL_ADMIN_PASSWORD:
                return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
            # #2: 채널 락으로 close 임계 구역 전체(제거 await + 상태 전이)를 직렬화한다 —
            # RemoveParticipant 와 close 사이에 publish/takeover 의 새 lease 발급이 끼어들지
            # 못하게 한다.
            async with st.channel_lock(channel_id):
                # 락 획득 후 상태 재확인(락 대기 중 다른 코루틴이 이미 종료/인수했을 수 있음).
                ch = st.db.get_channel(channel_id)
                if ch is None or ch.state != "open":
                    return _err("not_found", "채널을 찾을 수 없습니다.", 404)
                # fail-open 방지: RemoveParticipant 성공 후에만 상태 전이(lease 해제·종료 표시).
                lease = st.db.get_lease(channel_id)
                if lease is not None:
                    removed = await _try_remove(st, lease.identity)
                    if not removed:
                        return _err("livekit_error", "LiveKit 참가자 제거에 실패했습니다. 다시 시도하세요.", 502)
                st.db.close_channel(channel_id)
                st.on_air.clear_channel(channel_id)
        return Response(status_code=204)

    # ---- subscribe tokens ----
    @app.post("/channels/{channel_id}/subscribe-tokens")
    async def issue_subscribe(request: Request, channel_id: int = Path(ge=0, le=15)):
        st = _st(request)
        ip = _ip(request, st)
        ok, retry = st.subscribe_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, retry)
        ch = st.db.get_channel(channel_id)
        if ch is None:
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)
        if ch.state != "open":
            return _err("channel_closed", "종료된 채널입니다.", 409)
        # 회전 락(14차 #1): 세대 회전 중 구세대 기준 ensure_room 이 삭제된 구세대 룸을
        # 재생성해 기존 JWT 가 되살아나는 것을 막는다. 락 안에서는 세대가 바뀌지 않는다.
        async with st.rotation_lock:
            # 룸이 empty_timeout 등으로 사라졌으면 재생성해 발급 토큰이 join 실패하지 않도록 보장.
            await st.ensure_room()
            token, identity = issue_subscribe_token(
                st.settings.livekit_api_key, st.settings.livekit_api_secret, st.generation, channel_id
            )
            listener_id = st.db.issue_listener(channel_id)
            st.record_signal_event(
                direction="receive",
                event_type="token_issued",
                scope="relay",
                channel_id=channel_id,
                room=st.room,
                # JWT participant identity를 해시해 실제 joined 웹훅과 연계한다.
                # heartbeat용 listener_id는 별도 원장 식별자라 둘을 혼동하지 않는다.
                subject=identity,
                client_ip=ip,
            )
            return subscribe_response(token, st.generation, channel_id, st.settings.ws_url, listener_id)

    # ---- heartbeat ----
    @app.post("/listeners/heartbeat", status_code=204)
    async def heartbeat(request: Request, body: HeartbeatBody = Body(...)):
        st = _st(request)
        # (1) 발급 원장 확인을 **먼저** — 미발급 listener_id 는 limiter 키를 만들지 않고 즉시 무시.
        issued_channel = st.db.issued_listener_channel(body.listener_id)
        if issued_channel is None:
            return Response(status_code=204)
        # (2) 발급된 채널과 보고 채널 대조 — 불일치면 조용히 무시(계수 조작 방지).
        if issued_channel != body.channel_id:
            return Response(status_code=204)
        # (3) 발급 확인된 listener 만 rate limiter 적용.
        ok, retry = st.heartbeat_rl.check(body.listener_id)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, retry)
        ch = st.db.get_channel(body.channel_id)
        if ch is None:
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)
        st.db.record_heartbeat(body.listener_id, body.channel_id, LISTENER_HEARTBEAT_TTL_SECONDS)
        return Response(status_code=204)

    # ---- publish tokens ----
    @app.post("/publish-tokens")
    async def issue_publish(
        request: Request,
        body: PublishTokenBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.publish_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다. 잠시 후 다시 시도하세요.", 423, retry)
        ok, rretry = st.publish_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.send_password):
            st.publish_lock.record_failure(ip)
            return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
        st.publish_lock.record_success(ip)

        ch = st.db.get_channel(body.channel_id)
        if ch is None or ch.state != "open":
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)

        # 회전 락(14차): 인증~ensure_room~lease 발급 임계구역 전체에서 자격증명 회전을
        # 배제한다. 락 순서는 회전 락 → 채널 락(역순 없음 → 데드락 불가).
        async with st.rotation_lock:
            # 락 획득 후 인증 재검증: 대기 중 송신자 비밀번호가 회전되었으면 거부한다.
            if not _auth_bearer(authorization, st.send_password):
                return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
            # #2: 채널 락으로 lease 발급 임계 구역(존재 확인·present 조회·acquire)을 직렬화한다 —
            # close/takeover 의 RemoveParticipant~상태 전이 도중에 새 lease 가 발급되지 않게 한다.
            async with st.channel_lock(body.channel_id):
                # 락 획득 후 채널 상태 재확인(대기 중 종료됐을 수 있음).
                ch = st.db.get_channel(body.channel_id)
                if ch is None or ch.state != "open":
                    return _err("not_found", "채널을 찾을 수 없습니다.", 404)

                # 룸이 사라졌으면 재생성(모든 토큰 발급 경로에서 룸 존재 보장).
                await st.ensure_room()

                # #6②: 활성 송신 lease 만료 덮어쓰기 방지 + 고착 joined lease 해제 —
                # 접속 완료(joined) lease 의 participant 가 아직 룸에 남아 있으면(활성 송신)
                # TTL 이 만료됐어도 자동 연장하고 후발 요청은 409. 반대로 LiveKit 재시작 등으로
                # 룸에 실존하지 않으면(joined 이지만 부재) lease 를 해제해 재발급을 허용한다
                # (그렇지 않으면 acquire_lease 가 joined+TTL 미래를 alive 로 보아 영구 409).
                existing = st.db.get_lease(body.channel_id)
                if existing is not None and existing.joined_at is not None:
                    if await _publisher_present(st, existing.identity):
                        st.db.extend_lease_if_identity(body.channel_id, existing.identity, PUBLISH_TTL_SECONDS)
                        return _err("channel_busy", "이미 송신 중인 채널입니다. 관리자 인수만 가능합니다.", 409)
                    # joined 이지만 룸에 없음 → 고착 lease 해제 후 새 발급 허용(#6②).
                    st.db.release_lease_if_identity(body.channel_id, existing.identity)

                nonce = new_nonce()
                acquired, identity = st.db.acquire_lease(
                    body.channel_id, ch.epoch, st.generation, nonce, PUBLISH_TTL_SECONDS, LEASE_JOIN_GRACE_SECONDS
                )
                if not acquired:
                    return _err("channel_busy", "이미 송신 중인 채널입니다. 관리자 인수만 가능합니다.", 409)
                token = issue_publish_token(
                    st.settings.livekit_api_key, st.settings.livekit_api_secret, st.generation, identity
                )
                st.record_signal_event(
                    direction="send",
                    event_type="token_issued",
                    scope="relay",
                    channel_id=body.channel_id,
                    room=st.room,
                    subject=identity,
                    client_ip=ip,
                )
                return publish_response(token, st.generation, body.channel_id, st.settings.ws_url, identity, ch.epoch)

    # ---- intercom tokens ----
    @app.post("/intercom-tokens")
    async def issue_intercom(
        request: Request,
        body: IntercomTokenBody = Body(default=IntercomTokenBody()),
        authorization: str | None = Header(default=None),
    ):
        """인터컴(PTT) 토큰 발급 — 송신자 비밀번호 인증, 전용 룸 intercom-g<gen>.

        publish-tokens 와 동일한 로그인 보호(https 강제·잠금·율제한)를 적용한다.
        참가 상한(8명)은 발급 시점의 룸 참가자 수로 검사한다(표시·자원 보호용 근사 —
        동시 발급 경쟁으로 순간 초과될 수 있으나 보안 경계는 송신 비밀번호다).
        """
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.publish_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다. 잠시 후 다시 시도하세요.", 423, retry)
        ok, rretry = st.publish_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.send_password):
            st.publish_lock.record_failure(ip)
            return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
        st.publish_lock.record_success(ip)

        # 회전 락: 세대 회전 중 구세대 인터컴 룸 재생성·구세대 기준 발급을 막는다.
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.send_password):
                return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
            try:
                await st.ensure_intercom_room()
                participants = await st.livekit.list_participants(st.intercom_room)
            except Exception:
                # 조회 실패를 0명으로 간주하면 상한이 우회되므로 발급을 거부한다(fail-closed).
                return _err("livekit_error", "인터컴 상태 확인에 실패했습니다. 다시 시도하세요.", 502)
            if _intercom_user_count(participants) >= INTERCOM_MAX_PARTICIPANTS:
                return _err(
                    "intercom_full",
                    f"인터컴 정원({INTERCOM_MAX_PARTICIPANTS}명)이 가득 찼습니다.",
                    409,
                )
            token, identity, track = issue_intercom_token(
                st.settings.livekit_api_key,
                st.settings.livekit_api_secret,
                st.generation,
                body.name,
            )
            st.record_signal_event(
                direction="both",
                event_type="token_issued",
                scope="intercom",
                room=st.intercom_room,
                track_name=track,
                subject=identity,
                client_ip=ip,
            )
            return intercom_response(token, st.generation, st.settings.ws_url, identity, track)

    # ---- intercom channels (무전기 채널) ----
    def _intercom_auth(request: Request, authorization: str | None):
        """무전기 채널 API 공통 가드: https 강제 + 잠금/율제한 + 송신자 비밀번호 인증.

        통과 시 None, 실패 시 오류 응답을 반환한다. 송신 계열과 동일한 보호를 쓴다.
        """
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.publish_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다. 잠시 후 다시 시도하세요.", 423, retry)
        ok, rretry = st.publish_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.send_password):
            st.publish_lock.record_failure(ip)
            return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
        st.publish_lock.record_success(ip)
        return None

    def _intercom_channel_view(st: AppState, row) -> dict:
        return {
            "channel_id": row.channel_id,
            "name": row.name,
            "has_password": row.has_password,
        }

    @app.get("/intercom/channels")
    async def list_intercom_channels(
        request: Request, authorization: str | None = Header(default=None)
    ):
        """무전기 채널 목록 — 송신자 비밀번호 인증(무전기 진입 검증 겸용)."""
        st = _st(request)
        denied = _intercom_auth(request, authorization)
        if denied is not None:
            return denied
        channels = [_intercom_channel_view(st, r) for r in st.db.list_intercom_channels()]
        return {"channels": channels, "generation": st.generation, "max_channels": INTERCOM_MAX_CHANNELS}

    @app.post("/intercom/channels", status_code=201)
    async def create_intercom_channel(
        request: Request,
        body: IntercomChannelCreateBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        """무전기 채널 개설 — 이름(필수)·비밀번호(선택). 가장 낮은 빈 슬롯(0~7)에 배정."""
        st = _st(request)
        denied = _intercom_auth(request, authorization)
        if denied is not None:
            return denied
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.send_password):
                return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
            cid = st.db.lowest_free_intercom_slot(INTERCOM_MAX_CHANNELS)
            if cid is None:
                return _err(
                    "max_channels_reached",
                    f"무전기 채널이 최대 개수({INTERCOM_MAX_CHANNELS})에 도달했습니다.",
                    409,
                )
            pw_hash = _channel_password_hash(body.password) if body.password else None
            try:
                await st.ensure_intercom_channel_room(cid)
            except Exception:
                return _err("livekit_error", "채널 룸 생성에 실패했습니다. 다시 시도하세요.", 502)
            try:
                row = st.db.create_intercom_channel(cid, body.channel_name, pw_hash)
            except ChannelExists:
                return _err("max_channels_reached", "이미 개설된 채널입니다.", 409)
            return _intercom_channel_view(st, row)

    @app.post("/intercom/channels/{channel_id}/tokens")
    async def enter_intercom_channel(
        request: Request,
        channel_id: int = Path(ge=0, le=INTERCOM_MAX_CHANNELS - 1),
        body: IntercomChannelEnterBody = Body(default=IntercomChannelEnterBody()),
        authorization: str | None = Header(default=None),
    ):
        """무전기 채널 입장 토큰 — 송신자 비밀번호 + (채널에 비번이 있으면) 채널 비번."""
        import asyncio

        st = _st(request)
        denied = _intercom_auth(request, authorization)
        if denied is not None:
            return denied

        # ── 채널 비밀번호 검증(23차) ──────────────────────────────────
        # rotation_lock **밖에서**, (IP:channel) per-key asyncio 락으로 직렬화해
        # is_locked~scrypt 검증~record 를 원자화한다(22차 #1 동시성 강화). scrypt 는
        # 이벤트루프를 막지 않도록 세마포어 제한 to_thread 로 실행한다(23차 신규 —
        # 전역 rotation_lock 안 동기 scrypt 로 전체 서버가 멈추던 문제 제거).
        pw_key = f"{_ip(request, st)}:{channel_id}"
        async with st.intercom_pw_key_lock(pw_key):
            locked, cretry = st.intercom_pw_lock.is_locked(pw_key)
            if locked:
                return _err("locked", "채널 비밀번호 실패가 반복되어 일시 잠금되었습니다.", 423, cretry)
            row = st.db.get_intercom_channel(channel_id)
            if row is None:
                return _err("not_found", "채널을 찾을 수 없습니다.", 404)
            # 검증 시점의 세대·해시를 캡처한다(24차 TOCTOU): 발급 직전 동일성을 재확인해,
            # 검증~발급 사이 세대 회전으로 같은 id 에 다른 비번 채널이 재생성돼도 미검증
            # 토큰이 나가지 않게 한다.
            verified_gen = st.generation
            verified_hash = row.password_hash
            if row.password_hash is not None:
                ok = False
                if body.password:
                    async with st.intercom_scrypt_sem:
                        ok = await asyncio.to_thread(
                            _channel_password_matches, body.password, row.password_hash
                        )
                if not ok:
                    st.intercom_pw_lock.record_failure(pw_key)
                    return _err("invalid_channel_password", "채널 비밀번호가 올바르지 않습니다.", 401)
                st.intercom_pw_lock.record_success(pw_key)

        # ── 토큰 발급(회전 락으로 세대 정합 보장, 비번 검증은 위에서 완료) ──
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.send_password):
                return _err("invalid_password", "비밀번호가 올바르지 않습니다.", 401)
            # 회전 대기 사이 채널이 폐기(세대 회전)됐을 수 있으므로 재확인 + 검증 시점과
            # 세대·비번 해시가 동일한지 대조한다(24차 TOCTOU). 불일치면 발급을 거부한다.
            row = st.db.get_intercom_channel(channel_id)
            if row is None:
                return _err("not_found", "채널을 찾을 수 없습니다.", 404)
            if st.generation != verified_gen or row.password_hash != verified_hash:
                return _err("channel_changed", "채널 상태가 변경되었습니다. 다시 시도하세요.", 409)
            room = st.intercom_channel_room(channel_id)
            try:
                await st.ensure_intercom_channel_room(channel_id)
                participants = await st.livekit.list_participants(room)
            except Exception:
                return _err("livekit_error", "채널 상태 확인에 실패했습니다. 다시 시도하세요.", 502)
            if _intercom_user_count(participants) >= INTERCOM_MAX_PARTICIPANTS:
                return _err(
                    "intercom_full",
                    f"채널 정원({INTERCOM_MAX_PARTICIPANTS}명)이 가득 찼습니다.",
                    409,
                )
            token, identity, track = issue_intercom_token(
                st.settings.livekit_api_key,
                st.settings.livekit_api_secret,
                st.generation,
                body.name,
                room=room,
            )
            st.record_signal_event(
                direction="both",
                event_type="token_issued",
                scope="intercom",
                channel_id=channel_id,
                room=room,
                track_name=track,
                subject=identity,
                client_ip=_ip(request, st),
            )
            return intercom_response(
                token, st.generation, st.settings.ws_url, identity, track,
                room=room, channel_id=channel_id,
            )

    # ---- status ----
    @app.get("/status")
    async def get_status(request: Request):
        st = _st(request)
        st.db.purge_expired_heartbeats()
        st.db.purge_expired_issued_listeners()
        use_hb = st.db.has_any_live_heartbeat()
        channels = []
        total = 0
        for c in st.db.list_channels():
            v = _channel_view(st, c)
            total += v["listeners"]
            channels.append(
                {
                    "channel_id": c.channel_id,
                    "language": c.language,
                    "label": c.label,
                    "state": c.state,
                    "on_air": v["on_air"],
                    "listeners": v["listeners"],
                }
            )
        return {
            "generation": st.generation,
            "total_listeners": total,
            "listeners_source": "heartbeat" if use_hb else "token_approximation",
            "channels": channels,
        }

    # ---- admin ----
    @app.get("/admin/status")
    async def admin_status(request: Request, authorization: str | None = Header(default=None)):
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.admin_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다.", 423, retry)
        ok, rretry = st.admin_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.admin_password):
            st.admin_lock.record_failure(ip)
            return _err("unauthorized", "인증이 필요합니다.", 401)
        st.admin_lock.record_success(ip)

        st.db.purge_expired_heartbeats()
        st.db.purge_expired_issued_listeners()
        channels = []
        total = 0
        for c in st.db.list_channels():
            v = _channel_view(st, c)
            total += v["listeners"]
            lease = st.db.get_lease(c.channel_id)
            import datetime as _dt

            def _iso(ts):
                return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat() if ts else None

            channels.append(
                {
                    "channel_id": c.channel_id,
                    "language": c.language,
                    "label": c.label,
                    "state": c.state,
                    "on_air": v["on_air"],
                    "listeners": v["listeners"],
                    "publisher_identity": lease.identity if lease else None,
                    "publisher_joined_at": _iso(lease.joined_at) if lease else None,
                    "epoch": c.epoch,
                    "lease_held": lease is not None,
                    "lease_expires_at": _iso(lease.expires_at) if lease else None,
                }
            )
        return {"generation": st.generation, "total_listeners": total, "channels": channels}

    @app.post("/admin/channels/{channel_id}/takeover")
    async def takeover(
        request: Request,
        channel_id: int = Path(ge=0, le=15),
        authorization: str | None = Header(default=None),
    ):
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.admin_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다.", 423, retry)
        ok, rretry = st.admin_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.admin_password):
            st.admin_lock.record_failure(ip)
            return _err("unauthorized", "인증이 필요합니다.", 401)
        st.admin_lock.record_success(ip)
        # 초기 관리자 비밀번호(0000)로는 특권 작업을 허용하지 않는다(13차 #3).
        if st.admin_password == INITIAL_ADMIN_PASSWORD:
            return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)

        ch = st.db.get_channel(channel_id)
        if ch is None or ch.state != "open":
            return _err("not_found", "채널을 찾을 수 없습니다.", 404)

        # 회전 락(14차 #3): 인증 재검증부터 제거·epoch 증가·lease 재획득·토큰 발급까지
        # 자격증명 회전을 배제한다 — 구 관리자 비밀번호 요청이 송신자를 끊는 것을 막는다.
        async with st.rotation_lock:
            # 락 획득 후 인증·초기 비밀번호 게이트 재검증(14차 #2).
            if not _auth_bearer(authorization, st.admin_password):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            if st.admin_password == INITIAL_ADMIN_PASSWORD:
                return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
            # #2: 채널 락으로 takeover 임계 구역 전체(제거 await + epoch 증가 + lease 재획득)를
            # 직렬화한다 — 제거~재획득 사이에 publish 의 새 lease 발급이 끼어들지 못하게 한다.
            async with st.channel_lock(channel_id):
                # 락 획득 후 채널 상태 재확인.
                ch = st.db.get_channel(channel_id)
                if ch is None or ch.state != "open":
                    return _err("not_found", "채널을 찾을 수 없습니다.", 404)

                # 룸 존재 보장(모든 토큰 발급 경로).
                await st.ensure_room()

                # (1) 기존 송신자 제거 — fail-open 방지: 제거 성공 후에만 epoch 증가·lease 재획득.
                old_lease = st.db.get_lease(channel_id)
                if old_lease is not None:
                    removed = await _try_remove(st, old_lease.identity)
                    if not removed:
                        return _err("livekit_error", "기존 송신자 제거에 실패했습니다. 다시 시도하세요.", 502)
                # (2) channel epoch +1.
                new_epoch = st.db.bump_channel_epoch(channel_id)
                # (3) 기존 lease 해제 + 새 nonce 로 새 lease.
                nonce = new_nonce()
                identity = st.db.force_acquire_lease(
                    channel_id, new_epoch, st.generation, nonce, PUBLISH_TTL_SECONDS
                )
                st.on_air.clear_channel(channel_id)
                token = issue_publish_token(
                    st.settings.livekit_api_key, st.settings.livekit_api_secret, st.generation, identity
                )
                st.record_signal_event(
                    direction="send",
                    event_type="token_issued",
                    scope="relay",
                    channel_id=channel_id,
                    room=st.room,
                    subject=identity,
                    client_ip=ip,
                )
                return publish_response(token, st.generation, channel_id, st.settings.ws_url, identity, new_epoch)

    def _admin_guard(request: Request, authorization: str | None, allow_initial: bool = False):
        """admin 공통 가드: https 강제 + 잠금/율제한 + 관리자 비밀번호 인증.

        실패 시 오류 응답을 반환하고, 통과 시 None 을 반환한다.
        allow_initial=False 면 초기 관리자 비밀번호(0000) 상태에서 403 을 반환해,
        비밀번호 변경(과 로그인용 상태 조회) 외의 관리자 작업을 서버에서 차단한다(13차 #3).
        """
        st = _st(request)
        guard = _https_guard(request, st)
        if guard is not None:
            return guard
        ip = _ip(request, st)
        locked, retry = st.admin_lock.is_locked(ip)
        if locked:
            return _err("locked", "실패가 반복되어 일시 잠금되었습니다.", 423, retry)
        ok, rretry = st.admin_rl.check(ip)
        if not ok:
            return _err("rate_limited", "시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429, rretry)
        if not _auth_bearer(authorization, st.admin_password):
            st.admin_lock.record_failure(ip)
            return _err("unauthorized", "인증이 필요합니다.", 401)
        st.admin_lock.record_success(ip)
        if not allow_initial and st.admin_password == INITIAL_ADMIN_PASSWORD:
            return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
        return None

    @app.put("/admin/passwords/admin", status_code=204)
    async def change_admin_password(
        request: Request,
        body: PasswordChangeBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        """관리자 비밀번호 변경. 세대는 유지된다(방송 중단 없음). 초기값은 .env(권장 0000)."""
        st = _st(request)
        # 초기 비밀번호(0000) 상태에서도 "변경"만은 허용해야 한다(강제 변경 경로).
        denied = _admin_guard(request, authorization, allow_initial=True)
        if denied is not None:
            return denied
        # 회전 락 획득 후 인증 재검증(14차 #2): 대기 중 관리자 비밀번호가 이미 변경되었으면
        # (예: 초기 0000 을 먼저 온 요청이 변경) 구 비밀번호 요청이 덮어쓰지 못하게 한다.
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.admin_password):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            try:
                st.change_admin_password_locked(body.new_password)
            except ValueError as exc:
                return _err("invalid_password", str(exc), 400)
        return Response(status_code=204)

    @app.put("/admin/passwords/send", status_code=204)
    async def change_send_password(
        request: Request,
        body: PasswordChangeBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        """송신자 비밀번호 변경. 세대가 회전되어 기존 토큰·룸·lease 가 전부 폐기된다(계약)."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        # 회전 락 획득 후 인증·초기 비밀번호 게이트 재검증(14차 #2).
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.admin_password):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            if st.admin_password == INITIAL_ADMIN_PASSWORD:
                return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
            # 실패 원자성(12차 #1): LiveKit 룸 회전이 실패하면 비밀번호·세대는 변경 전
            # 그대로다. 동일값 검증(ValueError)은 락 보유 중 수행된다(13차 #2).
            try:
                await st.change_send_password_locked(body.new_password)
            except ValueError as exc:
                return _err("invalid_password", str(exc), 400)
            except Exception:
                return _err("livekit_error", "룸 회전에 실패했습니다. 비밀번호는 변경되지 않았습니다. 다시 시도하세요.", 502)
        return Response(status_code=204)

    @app.post("/admin/monitor-tokens")
    async def issue_monitor(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """관리자 모니터 토큰 발급 — 대시보드 바로미터용 hidden participant(구독·발행 불가).

        초기 관리자 비밀번호(0000) 상태에서는 룸 접속 자격을 주지 않는다(_admin_guard).
        """
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        # 회전 락: 세대 회전 중 구세대 기준 발급·룸 재생성을 막는다(subscribe 경로와 동일).
        async with st.rotation_lock:
            # 락 획득 후 인증·초기 비밀번호 게이트 재검증(14차 #2).
            if not _auth_bearer(authorization, st.admin_password):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            if st.admin_password == INITIAL_ADMIN_PASSWORD:
                return _err("must_change_password", "초기 관리자 비밀번호를 먼저 변경해야 합니다.", 403)
            await st.ensure_room()
            token, identity = issue_monitor_token(
                st.settings.livekit_api_key, st.settings.livekit_api_secret, st.generation
            )
            return monitor_response(token, st.generation, st.settings.ws_url, identity)

    @app.get("/admin/intercom/status")
    async def admin_intercom_status(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """관리자용 무전기 채널 현황. 기술 participant를 제외한 사용자만 집계한다."""
        import asyncio

        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied

        rows = st.db.list_intercom_channels()

        async def channel_status(row):
            room = st.intercom_channel_room(row.channel_id)
            try:
                participants = await st.livekit.list_participants(room)
                users = [
                    {
                        "identity": str(getattr(p, "identity", "")),
                        "name": str(getattr(p, "name", "") or ""),
                    }
                    for p in participants
                    if str(getattr(p, "identity", "")).startswith("intercom-")
                ]
                return {
                    "channel_id": row.channel_id,
                    "name": row.name,
                    "participants": users,
                    "participant_count": len(users),
                    "available": True,
                }
            except Exception:
                return {
                    "channel_id": row.channel_id,
                    "name": row.name,
                    "participants": [],
                    "participant_count": 0,
                    "available": False,
                }

        channels = await asyncio.gather(*(channel_status(row) for row in rows))
        return {
            "channels": channels,
            "generation": st.generation,
            "total_participants": sum(c["participant_count"] for c in channels),
        }

    @app.post("/admin/intercom/channels/{channel_id}/monitor-tokens")
    async def issue_intercom_monitor(
        request: Request,
        channel_id: int = Path(ge=0, le=INTERCOM_MAX_CHANNELS - 1),
        authorization: str | None = Header(default=None),
    ):
        """무전기 채널 바로미터용 hidden·subscribe-only 관리자 토큰을 발급한다."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        async with st.rotation_lock:
            if not _auth_bearer(authorization, st.admin_password):
                return _err("unauthorized", "인증이 필요합니다.", 401)
            row = st.db.get_intercom_channel(channel_id)
            if row is None:
                return _err("not_found", "무전기 채널을 찾을 수 없습니다.", 404)
            await st.ensure_intercom_channel_room(channel_id)
            room = st.intercom_channel_room(channel_id)
            token, identity = issue_monitor_token(
                st.settings.livekit_api_key,
                st.settings.livekit_api_secret,
                st.generation,
                target_room=room,
            )
            return monitor_response(
                token,
                st.generation,
                st.settings.ws_url,
                identity,
                target_room=room,
                channel_id=channel_id,
            )

    @app.get("/admin/recordings")
    async def list_recordings(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """진행 중 녹음과 서버에 저장된 MP3 목록을 반환한다."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        return st.recordings.list()

    @app.post("/admin/recordings", status_code=201)
    async def start_recording(
        request: Request,
        body: RecordingStartBody = Body(...),
        authorization: str | None = Header(default=None),
    ):
        """방송 또는 무전기 채널의 서버 측 MP3 녹음을 시작한다."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied

        if body.kind == "relay":
            ch = st.db.get_channel(body.channel_id)
            if ch is None or ch.state != "open":
                return _err("not_found", "방송 채널을 찾을 수 없습니다.", 404)
            room = st.room
            label = ch.label
            track_name = f"ch-{body.channel_id:02d}"
            await st.ensure_room()
        else:
            if body.channel_id >= INTERCOM_MAX_CHANNELS:
                return _err("not_found", "무전기 채널을 찾을 수 없습니다.", 404)
            ch = st.db.get_intercom_channel(body.channel_id)
            if ch is None:
                return _err("not_found", "무전기 채널을 찾을 수 없습니다.", 404)
            room = st.intercom_channel_room(body.channel_id)
            label = ch.name
            track_name = None
            await st.ensure_intercom_channel_room(body.channel_id)

        try:
            return await st.recordings.start(
                body.kind,
                body.channel_id,
                label,
                room,
                track_name=track_name,
            )
        except RecordingError as exc:
            status = 409 if "이미 녹음 중" in str(exc) else 502
            return _err("recording_error", str(exc), status)

    @app.post("/admin/recordings/{recording_id}/stop")
    async def stop_recording(
        request: Request,
        recording_id: str,
        authorization: str | None = Header(default=None),
    ):
        """진행 중 녹음을 마감하고 MP3 메타데이터를 확정한다."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        try:
            return await st.recordings.stop(recording_id)
        except RecordingError as exc:
            return _err("recording_error", str(exc), 404)

    @app.get("/admin/recordings/{recording_id}/download")
    async def download_recording(
        request: Request,
        recording_id: str,
        authorization: str | None = Header(default=None),
    ):
        """관리자 인증 후 완결된 MP3 파일을 다운로드한다."""
        st = _st(request)
        denied = _admin_guard(request, authorization)
        if denied is not None:
            return denied
        path = st.recordings.download_path(recording_id)
        if path is None:
            return _err("not_found", "녹음 파일을 찾을 수 없습니다.", 404)
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=f"ev211-{recording_id}.mp3",
        )

    # ---- webhook ----
    @app.post("/livekit/webhook")
    async def livekit_webhook(request: Request, authorization: str | None = Header(default=None)):
        processor: WebhookProcessor = request.app.state.webhook
        body = (await request.body()).decode("utf-8")
        if not authorization:
            return _err("unauthorized", "웹훅 서명이 필요합니다.", 401)
        try:
            event = processor.verify(body, authorization)
        except Exception:
            # 서명 불일치·만료는 401 로 폐기(재시도 유도 안 함).
            return _err("unauthorized", "웹훅 서명 검증에 실패했습니다.", 401)
        try:
            await processor.handle(event)
        except Exception:
            # 처리 실패는 event id 를 커밋하지 않고 5xx 로 반환 → LiveKit 재시도 유도.
            return _err("webhook_processing_failed", "웹훅 처리에 실패했습니다.", 503)
        return Response(status_code=200)


def _channel_full(st: AppState, ch) -> dict:
    import datetime as _dt

    view = _channel_view(st, ch)
    view["created_at"] = _dt.datetime.fromtimestamp(ch.created_at, tz=_dt.timezone.utc).isoformat()
    return view


async def _publisher_present(st: AppState, identity: str) -> bool:
    """해당 identity 의 participant 가 현재 룸에 남아 있는지 확인한다(#6②).

    LiveKit 조회 실패 시 보수적으로 '존재'로 간주해 활성 송신을 보호한다.
    """
    try:
        participants = await st.livekit.list_participants(st.room)
    except Exception:
        return True
    return any(getattr(p, "identity", None) == identity for p in participants)


async def _try_remove(st: AppState, identity: str) -> bool:
    """RemoveParticipant 를 시도하고 성공 여부를 반환한다(fail-open 방지용).

    이미 룸에 없어 발생하는 "참가자 없음"류 오류는 목표 상태(제거됨)와 동일하므로
    성공으로 간주한다. 그 외(연결 실패 등)는 실패로 보고해 호출부가 502 로 보상한다.
    """
    try:
        await st.livekit.remove_participant(st.room, identity)
        return True
    except Exception as exc:  # noqa: BLE001 — 메시지로 not-found 여부만 구분
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "no participant" in msg:
            return True
        return False


# uvicorn 진입점.
app = create_app()
