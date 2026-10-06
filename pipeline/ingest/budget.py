"""Daily request budgets for APIs with per-day limits (SAM.gov)."""

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from psycopg.rows import TupleRow


class BudgetExhausted(RuntimeError):
    def __init__(self, source: str, limit: int, reason: str | None = None) -> None:
        detail = reason or f"{limit} requests since 00:00 UTC"
        super().__init__(
            f"Daily request budget for {source} is used up ({detail}). "
            "The job will resume on its next run."
        )


def utc_midnight(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def requests_today(conn: psycopg.Connection[TupleRow], source: str, now: datetime) -> int:
    """Requests recorded in raw_files since 00:00 UTC. Failed requests count too."""
    row = conn.execute(
        "SELECT count(*) FROM raw_files WHERE source = %s AND fetched_at >= %s",
        (source, utc_midnight(now)),
    ).fetchone()
    return int(row[0]) if row else 0


class DailyBudget:
    """Raises BudgetExhausted before a request that would exceed the daily limit."""

    def __init__(
        self,
        conn: psycopg.Connection[TupleRow],
        source: str,
        limit: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conn = conn
        self._source = source
        self._limit = limit
        self._now = now

    def remaining(self) -> int:
        return max(0, self._limit - requests_today(self._conn, self._source, self._now()))

    def check(self) -> None:
        if self.remaining() <= 0:
            raise BudgetExhausted(self._source, self._limit)
