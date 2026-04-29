import asyncio
from dataclasses import dataclass

from recorder import ZoomRecorder


@dataclass
class RecordingSession:
    session_num: int   # 1-based, per-user sequential
    url: str
    recorder: ZoomRecorder
    task: asyncio.Task
    display_num: int


class SessionStore:
    def __init__(self) -> None:
        self._pending: dict[int, dict] = {}       # user_id → setup state
        self._sessions: dict[int, list] = {}      # user_id → list[RecordingSession]

    # ── pending (setup flow) ──────────────────────────────────────────────────

    def has_pending(self, user_id: int) -> bool:
        return user_id in self._pending

    def get_pending(self, user_id: int) -> dict:
        return self._pending.get(user_id, {})

    def set_pending(self, user_id: int, data: dict) -> None:
        self._pending[user_id] = data

    def update_pending(self, user_id: int, **kwargs) -> None:
        self._pending[user_id].update(kwargs)

    def pop_pending(self, user_id: int) -> dict:
        return self._pending.pop(user_id, {})

    # ── sessions ──────────────────────────────────────────────────────────────

    def active(self, user_id: int) -> list:
        """Active (is_recording=True) sessions for this user only."""
        return [s for s in self._sessions.get(user_id, []) if s.recorder.is_recording]

    def total_active(self) -> int:
        """Total active sessions across ALL users."""
        return sum(
            1
            for sessions in self._sessions.values()
            for s in sessions
            if s.recorder.is_recording
        )

    def prune(self, user_id: int) -> None:
        """Drop finished sessions so session_num restarts from 1 when all done."""
        if user_id in self._sessions:
            self._sessions[user_id] = [
                s for s in self._sessions[user_id] if s.recorder.is_recording
            ]

    def next_num(self, user_id: int) -> int:
        existing = self._sessions.get(user_id, [])
        return max((s.session_num for s in existing), default=0) + 1

    def add(self, user_id: int, session: RecordingSession) -> None:
        self._sessions.setdefault(user_id, []).append(session)

    def find(self, user_id: int, session_num: int) -> RecordingSession | None:
        return next(
            (s for s in self._sessions.get(user_id, [])
             if s.session_num == session_num and s.recorder.is_recording),
            None,
        )
