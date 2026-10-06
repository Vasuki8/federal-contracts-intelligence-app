"""HTTP with retries, exponential backoff, throttling and API-key redaction."""

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SECRET_PARAMS = frozenset({"api_key"})
REDACTED = "REDACTED"


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of `params` with secret values replaced, including in nested dicts and URLs."""
    return {k: _redact_value(k, v) for k, v in params.items()}


def _redact_value(key: str, value: Any) -> Any:
    if key.lower() in SECRET_PARAMS:
        return REDACTED
    if isinstance(value, Mapping):
        return redact_params(value)
    if isinstance(value, str) and "api_key=" in value.lower():
        return redact_url(value)
    return value


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, REDACTED if k.lower() in SECRET_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="[],~!")))


def redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class ApiError(RuntimeError):
    """A request failed for good: non-retryable status, or retries used up."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Recorder(Protocol):
    """Called once per attempt, so every request (even failed ones) leaves a record."""

    def __call__(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, Any],
        response: httpx.Response | None,
        error: Exception | None,
    ) -> None: ...


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 2.0
    max_delay: float = 120.0


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())


class HttpClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        retry: RetryPolicy | None = None,
        min_interval: float = 0.0,
        before_request: Callable[[], None] | None = None,
        recorder: Recorder | None = None,
        secrets: tuple[str, ...] = (),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._min_interval = min_interval
        self._before_request = before_request
        self._recorder = recorder
        self._secrets = secrets
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._last_start: float | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        query = dict(params or {})
        for attempt in range(1, self._retry.max_attempts + 1):
            if self._before_request is not None:
                self._before_request()
            self._throttle()
            try:
                response = self._client.request(method, url, params=query, json=json)
            except httpx.TransportError as exc:
                self._record(method, url, query, None, exc)
                if attempt == self._retry.max_attempts:
                    message = redact_text(f"{type(exc).__name__}: {exc}", self._secrets)
                    raise ApiError(f"{method} {redact_url(url)} failed: {message}") from exc
                self._sleep(self._backoff(attempt, None))
                continue
            self._record(method, url, query, response, None)
            if response.status_code in RETRY_STATUSES and attempt < self._retry.max_attempts:
                self._sleep(self._backoff(attempt, response))
                continue
            if response.is_error:
                raise self._error(method, url, response)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response:
        return self.request("GET", url, params=params)

    def post(self, url: str, *, json: Any) -> httpx.Response:
        return self.request("POST", url, json=json)

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_start is not None:
            wait = self._min_interval - (now - self._last_start)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_start = now

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            hinted = retry_after_seconds(response)
            if hinted is not None:
                return min(hinted, self._retry.max_delay)
        exponential = self._retry.base_delay * 2.0 ** (attempt - 1)
        return min(exponential * (0.5 + self._jitter()), self._retry.max_delay)

    def _record(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any],
        response: httpx.Response | None,
        error: Exception | None,
    ) -> None:
        if self._recorder is not None:
            self._recorder(
                method=method,
                url=redact_url(url),
                params=redact_params(params),
                response=response,
                error=error,
            )

    def _error(self, method: str, url: str, response: httpx.Response) -> ApiError:
        body = redact_text(response.text[:500], self._secrets)
        return ApiError(
            f"{method} {redact_url(url)} returned HTTP {response.status_code}: {body}",
            status=response.status_code,
        )
