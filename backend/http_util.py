"""Shared GET-with-retries for Upstox's REST APIs (stdlib only, so it adds no dependency).

Retries on HTTP 429, HTTP 5xx and connection failures/timeouts, with exponential backoff
(1s, 2s, 4s, 8s + jitter) unless the server names its own wait via Retry-After. Anything else
(400/401/403/404, ...) is not going to improve by retrying, so it is raised immediately.

Failures surface as urllib's own exceptions - HTTPError (status still readable, body still
readable via .read()) and URLError - so callers that already catch those keep working.
"""
import io
import json
import random
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_ATTEMPTS = 5
# Upstox sits behind Cloudflare, which rejects urllib's default "Python-urllib/x.y" signature with
# HTTP 403 (error 1010). Every request therefore carries a User-Agent unless the caller sets its own.
DEFAULT_USER_AGENT = "zero-upstox-client/1.0"


def request_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = 30,
    max_attempts: int = MAX_ATTEMPTS,
    pause: float = 0.0,
    log: Callable[[str], None] | None = None,
    label: str | None = None,
) -> Any:
    """GET `url` and return the decoded JSON body.

    `pause` is a sleep after each successful request, for callers that fire many requests in
    a row (a job walking hundreds of days) and want to stay under the rate limit instead of
    leaning on 429 backoff. Interactive callers leave it at 0.
    """
    what = label or url
    if not any(name.lower() == "user-agent" for name in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    for attempt in range(1, max_attempts + 1):
        retry_after: str | None = None
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as response:
                body = json.loads(response.read().decode())
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            problem = f"HTTP {error.code}"
            retry_after = error.headers.get("Retry-After") if error.headers else None
            # Keep the body readable on the exception we may re-raise: the original's stream is
            # consumed once read, and callers report the API's own reason text.
            failure: Exception = HTTPError(url, error.code, error.msg, error.headers, io.BytesIO(error.read() or b""))
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as error:
            problem = type(error).__name__
            failure = error if isinstance(error, URLError) else URLError(error)
        else:
            if pause > 0:
                time.sleep(pause)
            return body
        if attempt == max_attempts:
            raise failure
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt - 1) + random.random()
        if log:
            log(f"  {problem} on {what} (attempt {attempt}/{max_attempts}); retrying in {delay:.1f}s")
        time.sleep(delay)
    raise AssertionError("unreachable")
