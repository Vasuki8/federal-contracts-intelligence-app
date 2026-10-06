"""Client for the SAM.gov Get Opportunities API v2 (`/opportunities/v2/search`)."""

from collections.abc import Callable
from datetime import date
from types import TracebackType
from typing import Any, Self

import httpx

from pipeline.ingest.budget import BudgetExhausted, DailyBudget
from pipeline.ingest.context import IngestContext, ResponseArchiver
from pipeline.ingest.http import ApiError, HttpClient, RetryPolicy
from pipeline.ingest.opportunities import SOURCE

BASE_URL = "https://api.sam.gov/opportunities/v2/search"
PAGE_LIMIT = 1000  # docs: "Max Value = 1000"
EMPTY_PAGE: dict[str, Any] = {"totalRecords": 0, "opportunitiesData": []}


def sam_date(value: date) -> str:
    """The docs require MM/dd/yyyy."""
    return value.strftime("%m/%d/%Y")


class OpportunitiesClient:
    def __init__(
        self,
        http: HttpClient,
        api_key: str,
        archiver: ResponseArchiver | None = None,
        base_url: str = BASE_URL,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._archiver = archiver
        self._base_url = base_url
        self._on_close = on_close

    @property
    def last_raw_file_id(self) -> int | None:
        if self._archiver is None or self._archiver.last is None:
            return None
        return self._archiver.last.id

    def search(
        self,
        *,
        posted_from: date,
        posted_to: date,
        offset: int,
        limit: int = PAGE_LIMIT,
        ncode: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "postedFrom": sam_date(posted_from),
            "postedTo": sam_date(posted_to),
            "limit": str(limit),
            "offset": str(offset),
        }
        if ncode:
            params["ncode"] = ncode
        try:
            response = self._http.get(self._base_url, params={**params, "api_key": self._api_key})
        except ApiError as exc:
            if exc.status == 404:  # docs: "404 - No Data found"
                return dict(EMPTY_PAGE)
            if exc.status == 429:
                raise BudgetExhausted(SOURCE, 0, "SAM.gov answered 429 Too Many Requests") from exc
            raise
        payload = response.json()
        return payload if isinstance(payload, dict) else dict(EMPTY_PAGE)

    def close(self) -> None:
        if self._on_close is not None:
            self._on_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def build_client(
    ctx: IngestContext,
    api_key: str,
    daily_limit: int,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> OpportunitiesClient:
    """Wire archiving (every attempt), the daily budget and retries around the API."""
    archiver = ResponseArchiver(ctx, SOURCE, ".json", compress=True)
    budget = DailyBudget(ctx.meta, SOURCE, daily_limit)
    client = httpx.Client(timeout=httpx.Timeout(60.0), transport=transport)
    extra: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    http = HttpClient(
        client,
        retry=RetryPolicy(max_attempts=3),
        min_interval=1.0,
        before_request=budget.check,
        recorder=archiver,
        secrets=(api_key,),
        **extra,
    )
    return OpportunitiesClient(http, api_key, archiver, on_close=client.close)
