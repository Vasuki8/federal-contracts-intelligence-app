"""USAspending custom award download: request → poll status → stream the zip to the archive.

Endpoints (from the API contracts): POST /api/v2/download/awards/,
GET /api/v2/download/status?file_name=…  No API key.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx

from pipeline.ingest.archive import ArchivedFile, archive_bytes, archive_file
from pipeline.ingest.awards import SOURCE
from pipeline.ingest.context import IngestContext, ResponseArchiver
from pipeline.ingest.http import RETRY_STATUSES, ApiError, HttpClient, RetryPolicy

API_BASE = "https://api.usaspending.gov"
DOWNLOAD_PATH = "/api/v2/download/awards/"
STATUS_PATH = "/api/v2/download/status"
MAX_ROWS = 500_000  # settings.MAX_DOWNLOAD_LIMIT in usaspending-api
CONTRACT_TYPES = ("A", "B", "C", "D")
IDV_TYPES = ("IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E")

DateType = Literal["action_date", "last_modified_date"]


class DownloadFailed(RuntimeError):
    pass


def download_filters(
    start: date, end: date, naics: list[str], date_type: DateType
) -> dict[str, Any]:
    return {
        "award_type_codes": [*CONTRACT_TYPES, *IDV_TYPES],
        "naics_codes": {"require": naics},
        "time_period": [
            {"start_date": start.isoformat(), "end_date": end.isoformat(), "date_type": date_type}
        ],
    }


@dataclass(frozen=True)
class Download:
    file: ArchivedFile
    total_rows: int | None

    @property
    def truncated(self) -> bool:
        return self.total_rows is not None and self.total_rows >= MAX_ROWS


class AwardsClient:
    def __init__(
        self,
        ctx: IngestContext,
        requests: HttpClient,
        polls: HttpClient,
        stream: httpx.Client,
        *,
        base_url: str = API_BASE,
        sleep: Callable[[float], None] = time.sleep,
        poll_timeout: float = 3600.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._ctx = ctx
        self._requests = requests  # archives every attempt
        self._polls = polls  # status polls; only the final status is archived
        self._stream = stream
        self._base = base_url
        self._sleep = sleep
        self._poll_timeout = poll_timeout
        self._retry = retry or RetryPolicy()

    def start(self, filters: Mapping[str, Any], limit: int | None = None) -> str:
        """Ask USAspending to build the file; returns its file_name."""
        body: dict[str, Any] = {"filters": dict(filters), "file_format": "csv"}
        if limit is not None:
            body["limit"] = limit
        payload = self._requests.post(urljoin(self._base, DOWNLOAD_PATH), json=body).json()
        file_name = payload.get("file_name") if isinstance(payload, dict) else None
        if not file_name:
            raise DownloadFailed(f"Download request returned no file_name: {payload!r:.300}")
        return str(file_name)

    def wait(self, file_name: str) -> dict[str, Any]:
        """Poll until the file is finished; returns the final status payload."""
        delay, waited = 5.0, 0.0
        while True:
            response = self._polls.get(
                urljoin(self._base, STATUS_PATH), params={"file_name": file_name}
            )
            status = response.json()
            state = status.get("status") if isinstance(status, dict) else None
            if state == "finished":
                self._archive_status(response.content, file_name)
                return dict(status)
            if state == "failed":
                self._archive_status(response.content, file_name)
                raise DownloadFailed(
                    f"USAspending could not build {file_name}: {status.get('message')}"
                )
            if waited >= self._poll_timeout:
                raise DownloadFailed(f"{file_name} was not ready after {waited:.0f}s")
            self._sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 60.0)

    def fetch(self, status: Mapping[str, Any], request: Mapping[str, Any]) -> Download:
        """Stream the finished zip into the raw archive."""
        url = urljoin(self._base, str(status.get("file_url") or ""))
        tmp_dir = self._ctx.archive_root / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"{status.get('file_name', 'download')}.part"
        http_status = self._stream_to(url, tmp)
        archived = archive_file(
            self._ctx.meta,
            root=self._ctx.archive_root,
            source=SOURCE,
            file=tmp,
            suffix=".zip",
            run_id=self._ctx.run.id,
            http_status=http_status,
            request_params={"url": url, **request},
        )
        total = status.get("total_rows")
        return Download(file=archived, total_rows=int(total) if total is not None else None)

    def _stream_to(self, url: str, target: Path) -> int:
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                with self._stream.stream("GET", url) as response:
                    if (
                        response.status_code in RETRY_STATUSES
                        and attempt < self._retry.max_attempts
                    ):
                        raise _Retry
                    if response.is_error:
                        raise ApiError(
                            f"GET {url} returned HTTP {response.status_code}", response.status_code
                        )
                    with target.open("wb") as out:
                        for block in response.iter_bytes():
                            out.write(block)
                    self._ctx.run.counters.requests_made += 1
                    return response.status_code
            except (_Retry, httpx.TransportError) as exc:
                if attempt == self._retry.max_attempts:
                    raise ApiError(f"GET {url} failed: {exc!r}") from exc
                self._sleep(
                    min(self._retry.base_delay * 2.0 ** (attempt - 1), self._retry.max_delay)
                )
        raise AssertionError("unreachable")  # pragma: no cover

    def _archive_status(self, content: bytes, file_name: str) -> None:
        archive_bytes(
            self._ctx.meta,
            root=self._ctx.archive_root,
            source=SOURCE,
            content=content,
            suffix=".status.json",
            run_id=self._ctx.run.id,
            http_status=200,
            request_params={"file_name": file_name},
        )


class _Retry(Exception):
    pass


def build_client(
    ctx: IngestContext,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[AwardsClient, Callable[[], None]]:
    """The client plus a close() for its connections."""
    archiver = ResponseArchiver(ctx, SOURCE, ".json", compress=True)
    api = httpx.Client(timeout=httpx.Timeout(120.0), transport=transport)
    stream = httpx.Client(timeout=httpx.Timeout(600.0), transport=transport, follow_redirects=True)
    requests = HttpClient(api, min_interval=0.5, recorder=archiver, sleep=sleep)
    polls = HttpClient(api, min_interval=0.5, sleep=sleep)

    def close() -> None:
        api.close()
        stream.close()

    return AwardsClient(ctx, requests, polls, stream, sleep=sleep), close
