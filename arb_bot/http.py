"""Minimal HTTP helper: UA header, retries, polite rate limiting. Stdlib only."""
import json
import threading
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arb-research/0.1"}

_last_call = 0.0
_gap = 0.08
_lock = threading.Lock()  # GUI runs fetch + settle in separate threads


def set_request_gap(seconds: float):
    global _gap
    _gap = seconds


def get_json(url: str, retries: int = 3, timeout: int = 30):
    global _last_call
    for attempt in range(retries):
        with _lock:
            wait = _gap - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} retries: {url}")
