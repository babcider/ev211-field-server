# speaker identity 규약(speaker-ch-NN-e<epoch>-g<gen>-n<nonce>) 파싱·검증 유틸
from __future__ import annotations

import re
from dataclasses import dataclass

# speaker-ch-01-e1-g1-nA1b2C3 형식. nonce 는 영숫자.
_SPEAKER_RE = re.compile(
    r"^speaker-ch-(?P<ch>\d{2})-e(?P<epoch>\d+)-g(?P<gen>\d+)-n(?P<nonce>[A-Za-z0-9]+)$"
)


@dataclass(frozen=True)
class SpeakerIdentity:
    channel_id: int
    epoch: int
    generation: int
    nonce: str
    raw: str


def parse_speaker_identity(identity: str) -> SpeakerIdentity | None:
    """규약 형식이면 파싱, 아니면 None."""
    m = _SPEAKER_RE.match(identity)
    if not m:
        return None
    return SpeakerIdentity(
        channel_id=int(m.group("ch")),
        epoch=int(m.group("epoch")),
        generation=int(m.group("gen")),
        nonce=m.group("nonce"),
        raw=identity,
    )


def is_monitor_identity(identity: str) -> bool:
    """모니터 participant(monitor-*)는 강제·집계에서 제외한다."""
    return identity.startswith("monitor-")


def is_listener_identity(identity: str) -> bool:
    return identity.startswith("listener-")


def is_intercom_identity(identity: str) -> bool:
    """인터컴(PTT) participant(intercom-*)는 릴레이 룸 강제·집계 대상이 아니다."""
    return identity.startswith("intercom-")
