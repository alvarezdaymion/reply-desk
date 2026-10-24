"""Retry delays from SDK errors and HTTP responses, with no network calls."""

import datetime as dt
import io
import urllib.error
from contextlib import nullcontext
from email.message import Message
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from conftest import NOW

from reply_desk.http import Http, HttpError, raise_if_rate_limited, retry_delay


class SDKRateLimitError(Exception):
    status_code = 429

    def __init__(self, headers):
        super().__init__("synthetic private provider message")
        self.response = SimpleNamespace(headers=headers)


@pytest.mark.parametrize("headers,expected", [
    ({"Retry-After": "60"}, 60),
    ({"retry-after": " 12.5 "}, 12.5),
    ({"RETRY-AFTER": "15"}, 15),
    ({"Retry-After": "bad"}, 1),
    ({"Retry-After": "-2"}, 1),
    ({"Retry-After": "0"}, 1),
    ({"Retry-After": "NaN"}, 1),
    ({"Retry-After": "Infinity"}, 1),
    ({}, 1),
])
def test_sdk_retry_after_headers_control_the_delay(headers, expected):
    error = SDKRateLimitError(headers)
    assert retry_delay(error) == expected
    with pytest.raises(HttpError) as converted:
        raise_if_rate_limited(error)
    assert converted.value.retry_after == expected
    assert "private" not in str(converted.value)


@pytest.mark.parametrize("seconds,expected", [(75, 75), (-75, 1)])
def test_retry_after_http_dates_are_relative_to_the_current_time(seconds, expected):
    value = format_datetime(NOW + dt.timedelta(seconds=seconds), usegmt=True)
    error = SDKRateLimitError({"Retry-After": value})
    assert retry_delay(error, now=NOW) == expected


@pytest.mark.parametrize("raises_http_error", [False, True])
def test_default_transport_preserves_retry_after_without_exposing_response_text(monkeypatch, raises_http_error):
    headers = Message()
    headers["Retry-After"] = "60"
    raw = b"synthetic private response, not JSON"

    def open_response(*args, **kwargs):
        if raises_http_error:
            raise urllib.error.HTTPError("https://example.com/private", 429, "private message",
                                          headers, io.BytesIO(raw))
        return nullcontext(SimpleNamespace(status=429, headers=headers, read=lambda: raw))

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    with pytest.raises(HttpError) as error:
        Http().request("GET", "https://example.com/resource", label="Test call")
    assert error.value.retry_after == 60
    assert str(error.value) == "Test call returned HTTP 429"


@pytest.mark.parametrize("body_delay,header_delay,expected", [(5, "60", 60), (90, "60", 90), (5, "bad", 5)])
def test_http_uses_the_longer_valid_body_or_header_delay(body_delay, header_delay, expected):
    import json

    def transport(*args):
        return 429, json.dumps({"parameters": {"retry_after": body_delay}}).encode(), {"Retry-After": header_delay}

    with pytest.raises(HttpError) as error:
        Http(transport=transport).request("GET", "https://example.com/resource", label="Test call")
    assert error.value.retry_after == expected
