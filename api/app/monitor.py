# on-air 판정 — 계약의 숨김 모니터 participant 대신 lease+track_published 근사(구현 편차)
from __future__ import annotations

import threading


class OnAirTracker:
    """채널별 on-air(송신 여부) 근사 상태.

    계약(codex 4차)은 룸에 상주하는 숨김 모니터 participant(monitor-*)가
    active_speakers/audioLevel 이벤트로 **발화(오디오 레벨) 기반** on-air 를 산출하도록
    규정한다. 그러나 livekit-rtc 로 서버 프로세스에 숨김 participant 를 상주시키는 것은
    네이티브 WebRTC 스택 의존·연결 불안정 리스크가 커, 계약이 허용한 폴백(README '구현 편차'
    참조)에 따라 **lease 점유 + track_published 웹훅** 기반 근사로 on_air 를 판정한다.

    한계: 오디오 레벨을 보지 못하므로 '트랙을 발행 중'이면 on_air=true 로 본다. 무음
    마이크를 on-air 로 오탐할 수 있다(발화 여부 미반영). 후속 과제로 남긴다.
    """

    def __init__(self) -> None:
        self._publishing: set[int] = set()  # 트랙 발행 중인 채널
        self._lock = threading.Lock()

    def set_publishing(self, channel_id: int, publishing: bool) -> None:
        with self._lock:
            if publishing:
                self._publishing.add(channel_id)
            else:
                self._publishing.discard(channel_id)

    def is_on_air(self, channel_id: int) -> bool:
        with self._lock:
            return channel_id in self._publishing

    def clear(self) -> None:
        with self._lock:
            self._publishing.clear()

    def clear_channel(self, channel_id: int) -> None:
        with self._lock:
            self._publishing.discard(channel_id)
