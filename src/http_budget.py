"""
Shared request budget + response cache for the free third-party APIs.

Why this exists: every external provider here is on a free tier with a hard
rate limit (football-data.org allows 10 requests/minute), and before this
module three separate callers — the fixture board, prediction grading and
the odds lookup — each fired requests at that same quota with no
coordination. A single board refresh could spend 9 calls on fixtures while
grading spent 25 more on results, so both got HTTP 429 and the user saw an
empty board and a log that never graded.

Three things fix that:

  * `RateBudget` is a token bucket per provider. Callers block until a slot
    is free instead of firing and failing, so the quota is spent in order
    rather than wasted on requests that all 429 together.
  * Responses are cached on disk with a TTL, so a restart doesn't re-spend
    the whole quota re-fetching what it already had.
  * On failure the cache is served **stale** rather than propagating the
    error. A league whose refresh 429s keeps showing yesterday's fixtures
    (clearly labelled as stale) instead of vanishing from the board.

Everything degrades to "return what we have and say so" — no call site
should ever have to choose between an exception and an empty result.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import config

CACHE_DIR = config.DATA_DIR / "http_cache"


class RateBudget:
    """Token bucket: at most `max_calls` acquisitions per `per_seconds`.

    Shared across threads, since FastAPI runs sync endpoints in a worker
    pool and two requests can otherwise race straight through the limit.
    """

    def __init__(self, name: str, max_calls: int, per_seconds: float):
        self.name = name
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and (now - self._calls[0]) >= self.per_seconds:
            self._calls.popleft()

    def try_acquire(self) -> bool:
        """Non-blocking: take a slot if one is free, else return False."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            return False

    def acquire(self, max_wait: float = 0.0) -> bool:
        """Take a slot, waiting up to `max_wait` seconds for one to free up.

        `max_wait=0` makes this equivalent to `try_acquire`. Callers serving
        an HTTP request should pass a small budget (a couple of seconds) so a
        page never hangs behind a full bucket; background work can afford to
        wait out a whole window.
        """
        deadline = time.monotonic() + max_wait
        while True:
            if self.try_acquire():
                return True
            now = time.monotonic()
            if now >= deadline:
                return False
            with self._lock:
                self._prune(now)
                # Sleep until the oldest call ages out, or until the caller's
                # deadline, whichever comes first.
                wait = (self._calls[0] + self.per_seconds - now) if self._calls else 0.05
            time.sleep(max(0.05, min(wait, deadline - now)))

    def available(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return self.max_calls - len(self._calls)


# football-data.org's documented free-tier limit is 10 requests/minute.
# There are exactly 9 covered divisions, so the budget has to fit all 9 in one
# window or the last division alphabetically fails on every cold refresh —
# which is precisely what used to happen to Spain. 9 calls over a 65-second
# window keeps the whole board inside one refresh while staying under the
# real limit with margin for clock skew.
FOOTBALL_DATA = RateBudget("football-data.org", max_calls=9, per_seconds=65.0)

# TheSportsDB's shared free key is undocumented but throttles aggressively.
THESPORTSDB = RateBudget("thesportsdb", max_calls=12, per_seconds=60.0)

# Squiggle asks for courteous use rather than publishing a hard limit; its
# data changes at most a few times a day, so this is generous already.
SQUIGGLE = RateBudget("squiggle", max_calls=20, per_seconds=60.0)

# the-odds-api.com bills per request against a monthly quota.
ODDS_API = RateBudget("the-odds-api", max_calls=6, per_seconds=60.0)


@dataclass
class Fetched:
    """A response plus how it was obtained, so callers can be honest in the UI."""
    data: Any
    status: str          # "fresh" | "cached" | "stale" | "missing"
    age_seconds: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def is_stale(self) -> bool:
        return self.status == "stale"


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{digest}.json"


def _read_cache(key: str) -> tuple[Any, float] | tuple[None, None]:
    path = _cache_path(key)
    if not path.exists():
        return None, None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return blob.get("data"), float(blob.get("ts", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None, None


def _write_cache(key: str, data: Any) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "key": key, "data": data}), encoding="utf-8")
        tmp.replace(_cache_path(key))  # atomic, so a crash mid-write can't corrupt the cache
    except OSError:
        pass  # a cache we can't write is a slower app, not a broken one


def _raw_get(url: str, headers: dict, timeout: float) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_WAIT_RE = re.compile(r"[Ww]ait\s+(\d+)\s*second")


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    """How long the server says to wait, from Retry-After or from
    football-data.org's plain-English 429 body."""
    header = err.headers.get("Retry-After") if err.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        match = _WAIT_RE.search(err.read().decode("utf-8", "replace"))
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


def get_json(
    url: str,
    *,
    budget: RateBudget,
    cache_key: str | None = None,
    ttl: float = 900.0,
    headers: dict | None = None,
    timeout: float = 15.0,
    max_wait: float = 0.0,
    stale_ok: bool = True,
) -> Fetched:
    """Fetch JSON through `budget`, backed by the on-disk cache.

    Returns a `Fetched` rather than raising: a caller that can show stale
    data should not have to catch an exception to find that out. Order of
    preference is fresh cache, then a live call, then stale cache.
    """
    key = cache_key or url
    headers = {"User-Agent": "sports-predictor/1.0 (+https://predict-pro.up.railway.app)", **(headers or {})}
    started = time.monotonic()

    cached, cached_ts = _read_cache(key)
    age = (time.time() - cached_ts) if cached_ts else float("inf")
    if cached is not None and age < ttl:
        return Fetched(cached, "cached", age)

    if not budget.acquire(max_wait=max_wait):
        if cached is not None and stale_ok:
            return Fetched(cached, "stale", age, f"{budget.name} rate budget exhausted")
        return Fetched(None, "missing", age, f"{budget.name} rate budget exhausted")

    try:
        data = _raw_get(url, headers, timeout)
    except urllib.error.HTTPError as e:
        # A 429 from football-data.org carries the exact wait in its body
        # ("You reached your request limit. Wait 7 seconds."), and Retry-After
        # is standard elsewhere. Honouring it turns a hard failure into a short
        # pause, which matters because the alternative is a league silently
        # dropping off the board.
        if e.code == 429:
            wait = _retry_after_seconds(e)
            remaining = max_wait - (time.monotonic() - started)
            if wait is not None and 0 < wait <= remaining:
                time.sleep(wait + 0.5)
                try:
                    data = _raw_get(url, headers, timeout)
                    _write_cache(key, data)
                    return Fetched(data, "fresh", 0.0)
                except Exception as retry_err:
                    e = retry_err if isinstance(retry_err, urllib.error.HTTPError) else e
        detail = f"HTTP {getattr(e, 'code', '?')}" + (" (rate limited)" if getattr(e, "code", None) == 429 else "")
        if cached is not None and stale_ok:
            return Fetched(cached, "stale", age, detail)
        return Fetched(None, "missing", age, detail)
    except Exception as e:  # URLError, timeout, malformed JSON
        detail = f"{type(e).__name__}: {e}"
        if cached is not None and stale_ok:
            return Fetched(cached, "stale", age, detail)
        return Fetched(None, "missing", age, detail)

    _write_cache(key, data)
    return Fetched(data, "fresh", 0.0)


def budget_status() -> list[dict]:
    """Remaining quota per provider, for the dashboard's status surface."""
    return [
        {"provider": b.name, "available": b.available(),
         "limit": b.max_calls, "window_seconds": b.per_seconds}
        for b in (FOOTBALL_DATA, THESPORTSDB, SQUIGGLE, ODDS_API)
    ]
