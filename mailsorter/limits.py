import ipaddress
import threading
import time

from .config import is_loopback

MAX_TRACKED_ADDRESSES = 10_000
CLOUDFLARE_CLIENT_HEADER = "CF-Connecting-IP"


def client_address(peer: str | None, headers) -> str:
    peer = peer or ""
    if not is_loopback(peer):
        return peer
    forwarded = (headers.get(CLOUDFLARE_CLIENT_HEADER) or "").strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock=time.time, max_tracked: int = MAX_TRACKED_ADDRESSES):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.max_tracked = max_tracked
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def _forget_old(self, now: float) -> None:
        if len(self._windows) <= self.max_tracked:
            return
        for key in [key for key, (started, _) in self._windows.items() if now - started >= self.window_seconds]:
            del self._windows[key]
        while len(self._windows) > self.max_tracked:
            del self._windows[next(iter(self._windows))]

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self.clock()
            started, count = self._windows.get(key, (now, 0))
            if now - started >= self.window_seconds:
                started, count = now, 0
            if count >= self.limit:
                return False
            self._windows[key] = (started, count + 1)
            self._forget_old(now)
            return True
