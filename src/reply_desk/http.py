"""A tiny JSON-over-HTTPS client. Stdlib only.

`transport` is swappable so tests never touch the network. Errors name the request by a short
label chosen by the caller and never include headers or the URL, so a token in a header or in a
URL path (Telegram puts it there) can't reach a log, a card or the terminal.

Transports return (status, body), optionally followed by response headers. The standard-library
transport includes headers so Retry-After survives even when the response body is not JSON.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

Response = tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]]
Transport = Callable[[str, str, dict[str, str], bytes | None, float], Response]

USER_AGENT = "reply-desk/0.1 (+https://github.com/alvarezdaymion/reply-desk)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _positive_seconds(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return delay if math.isfinite(delay) and delay > 0 else 0.0


def header_delay(headers: Any, now: dt.datetime | None = None) -> float:
    if not hasattr(headers, "items"):
        return 0.0
    delays = [0.0]
    for key, value in headers.items():
        if str(key).lower() != "retry-after":
            continue
        delay = _positive_seconds(value)
        if not delay and isinstance(value, str):
            try:
                due = parsedate_to_datetime(value)
                if due.tzinfo is not None:
                    delay = _positive_seconds((due - (now or dt.datetime.now(dt.UTC))).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        delays.append(delay)
    return max(delays)


def retry_delay(error: Exception, *, now: dt.datetime | None = None) -> float:
    response = getattr(error, "response", None)
    delay = max(_positive_seconds(getattr(error, "retry_after", None)),
                header_delay(getattr(response, "headers", None), now))
    if delay:
        return delay
    return 1.0 if getattr(error, "status", getattr(error, "status_code", None)) == 429 else 0.0


def raise_if_rate_limited(error: Exception) -> None:
    delay = retry_delay(error)
    if delay:
        if isinstance(error, HttpError):
            raise error
        raise HttpError("model request rate limited", 429, delay) from None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes, Mapping[str, str]]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


class Http:
    def __init__(self, headers: dict[str, str] | None = None, transport: Transport | None = None,
                 timeout: float = 30.0):
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def request(self, method: str, url: str, *, label: str, params: dict[str, Any] | None = None,
                json_body: Any = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = dict(self.headers)
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        try:
            response = self.transport(method, url, headers, body, self.timeout)
            if len(response) == 2:
                status, raw = response
                response_headers = {}
            else:
                status, raw, response_headers = response
        except Exception as e:  # timeouts, refused connections, DNS: the type is enough
            raise HttpError("%s failed: %s" % (label, type(e).__name__)) from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if status >= 400:
            parameters = data.get("parameters") if isinstance(data, dict) else None
            body_delay = _positive_seconds(parameters.get("retry_after")) if isinstance(parameters, dict) else 0.0
            delay = max(body_delay, header_delay(response_headers)) or None
            raise HttpError("%s returned HTTP %d" % (label, status), status, delay)
        if data is None:
            raise HttpError("%s returned a body that is not JSON" % label, status)
        return data
