from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from pipeline.ingest.http import (
    REDACTED,
    ApiError,
    HttpClient,
    RetryPolicy,
    redact_params,
    redact_url,
    retry_after_seconds,
)

KEY = "sam-secret-key-123"


class Calls:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.records: list[dict[str, Any]] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def record(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, Any],
        response: httpx.Response | None,
        error: Exception | None,
    ) -> None:
        self.records.append(
            {"url": url, "params": dict(params), "status": response and response.status_code}
        )


def make_client(responses: list[httpx.Response | Exception], calls: Calls, **kw: Any) -> HttpClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return HttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        retry=RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=30.0),
        recorder=calls.record,
        secrets=(KEY,),
        sleep=calls.sleep,
        jitter=lambda: 0.5,
        **kw,
    )


def test_retries_server_errors_with_exponential_backoff() -> None:
    calls = Calls()
    client = make_client(
        [httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": True})], calls
    )
    response = client.get("https://api.example.test/x", params={"api_key": KEY})
    assert response.json() == {"ok": True}
    assert calls.sleeps == [1.0, 2.0]
    assert [r["status"] for r in calls.records] == [503, 502, 200]


def test_honours_retry_after_on_429() -> None:
    calls = Calls()
    client = make_client(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)], calls
    )
    client.get("https://api.example.test/x")
    assert calls.sleeps == [7.0]


def test_gives_up_after_max_attempts() -> None:
    calls = Calls()
    client = make_client([httpx.Response(503)] * 3, calls)
    with pytest.raises(ApiError) as excinfo:
        client.get("https://api.example.test/x")
    assert excinfo.value.status == 503
    assert len(calls.records) == 3


def test_client_errors_are_not_retried_and_never_leak_the_key() -> None:
    calls = Calls()
    body = f"An invalid api_key was supplied: {KEY}"
    client = make_client([httpx.Response(401, text=body)], calls)
    with pytest.raises(ApiError) as excinfo:
        client.get("https://api.example.test/x", params={"api_key": KEY})
    message = str(excinfo.value)
    assert KEY not in message
    assert "HTTP 401" in message
    assert calls.sleeps == []
    assert calls.records[0]["params"] == {"api_key": REDACTED}


def test_transport_errors_are_retried_and_recorded() -> None:
    calls = Calls()
    client = make_client([httpx.ConnectError("boom"), httpx.Response(200)], calls)
    client.get("https://api.example.test/x")
    assert [r["status"] for r in calls.records] == [None, 200]


def test_before_request_can_stop_a_request() -> None:
    calls = Calls()

    class Stop(Exception):
        pass

    def block() -> None:
        raise Stop

    client = make_client([httpx.Response(200)], calls, before_request=block)
    with pytest.raises(Stop):
        client.get("https://api.example.test/x")
    assert calls.records == []


def test_min_interval_spaces_requests() -> None:
    calls = Calls()
    ticks = iter([0.0, 0.25])
    client = make_client(
        [httpx.Response(200), httpx.Response(200)],
        calls,
        min_interval=1.0,
        clock=lambda: next(ticks),
    )
    client.get("https://api.example.test/a")
    client.get("https://api.example.test/b")
    assert calls.sleeps == [0.75]


def test_redaction_helpers() -> None:
    assert redact_params({"api_key": KEY, "limit": 5}) == {"api_key": REDACTED, "limit": 5}
    url = redact_url(f"https://api.sam.gov/opportunities/v2/search?api_key={KEY}&limit=5")
    assert KEY not in url
    assert "limit=5" in url


def test_retry_after_accepts_http_dates() -> None:
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert retry_after_seconds(response) == 0.0
