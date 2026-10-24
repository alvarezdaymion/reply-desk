"""A tiny JSON-over-HTTPS client. Stdlib only.

`transport` is swappable so tests never touch the network. Errors name the request by a short
label chosen by the caller and never include headers or the URL, so a token in a header or in a
URL path (Telegram puts it there) can't reach a log, a card or the terminal.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

USER_AGENT = "reply-desk/0.1 (+https://github.com/alvarezdaymion/reply-desk)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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
            status, raw = self.transport(method, url, headers, body, self.timeout)
        except Exception as e:  # timeouts, refused connections, DNS: the type is enough
            raise HttpError("%s failed: %s" % (label, type(e).__name__)) from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if status >= 400:
            parameters = data.get("parameters") if isinstance(data, dict) else None
            delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
                delay = None
            raise HttpError("%s returned HTTP %d" % (label, status), status, delay)
        if data is None:
            raise HttpError("%s returned a body that is not JSON" % label, status)
        return data
