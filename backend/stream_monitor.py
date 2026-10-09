import threading
from datetime import datetime
from typing import Any

from storage import append_gap_events, normalize_stream_message


class StreamMonitor:
    def __init__(self, gap_seconds: float = 5.0) -> None:
        self.gap_seconds = gap_seconds
        self._last_seen: dict[str, datetime] = {}
        self._gap_count = 0
        self._lock = threading.Lock()

    def record(self, message: Any, received_at: datetime) -> list[dict[str, Any]]:
        normalized = normalize_stream_message(message)
        instruments = self._instrument_keys(normalized)
        gaps: list[dict[str, Any]] = []
        with self._lock:
            for instrument_key in instruments:
                previous = self._last_seen.get(instrument_key)
                if previous is not None:
                    gap_seconds = (received_at - previous).total_seconds()
                    if gap_seconds > self.gap_seconds:
                        gap = {
                            "instrument_key": instrument_key,
                            "previous_received_at": previous.isoformat(),
                            "received_at": received_at.isoformat(),
                            "gap_seconds": round(gap_seconds, 3),
                            "threshold_seconds": self.gap_seconds,
                        }
                        self._gap_count += 1
                        gaps.append(gap)
                self._last_seen[instrument_key] = received_at
        append_gap_events(gaps, received_at)  # one write for the whole message, outside the lock
        return gaps

    def newest(self) -> datetime | None:
        """When the stream last delivered anything at all (any instrument): the heartbeat of the connection."""
        with self._lock:
            return max(self._last_seen.values(), default=None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tracked_instruments": len(self._last_seen),
                "last_seen": {
                    key: value.isoformat() for key, value in self._last_seen.items()
                },
                "gap_count": self._gap_count,
                "gap_threshold_seconds": self.gap_seconds,
            }

    @staticmethod
    def _instrument_keys(message: Any) -> list[str]:
        if not isinstance(message, dict):
            return ["__unknown__"]
        feeds = message.get("feeds")
        if isinstance(feeds, dict) and feeds:
            return [str(key) for key in feeds]
        for key_name in ("instrument_key", "instrumentKey", "instrument_token"):
            value = message.get(key_name)
            if value:
                return [str(value)]
        return ["__unknown__"]