import math
import threading
import time

LOGIN_COOLDOWN_SECONDS = 60
LOGIN_LIMIT = 5
_attempts = 0
_blocked_until = 0.0
_lock = threading.Lock()


def login_retry_after():
    global _attempts, _blocked_until
    now = time.monotonic()
    with _lock:
        if now < _blocked_until:
            return math.ceil(_blocked_until - now)
        _attempts += 1
        if _attempts == LOGIN_LIMIT:
            _attempts = 0
            _blocked_until = now + LOGIN_COOLDOWN_SECONDS
        return 0
