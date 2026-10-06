"""Save incumbent matches in bulk: COPY into a temp table, upsert the automatic rows that
changed, delete automatic rows the run no longer produces. Human decisions (`confirmed`,
`rejected`) are never touched."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from pipeline.match.candidates import Conn

TMP = "tmp_incumbent_matches"
_COLUMNS = ("notice_id", "award_key", "piid", "method", "score", "rank", "shown", "evidence")


@dataclass(frozen=True)
class MatchRow:
    notice_id: str
    award_key: str
    piid: str | None
    method: str
    score: float
    rank: int
    shown: str
    evidence: dict[str, Any]


def save_matches(
    conn: Conn, notice_ids: Sequence[str], rows: Iterable[MatchRow], matcher_version: str
) -> tuple[int, int]:
    """Replace the automatic incumbent matches of `notice_ids` with `rows`.
    Returns (written, deleted); re-running with the same rows writes nothing."""
    conn.execute(f"DROP TABLE IF EXISTS {TMP}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {TMP} (
            notice_id text, award_key text, piid text, method text, score numeric(5, 4),
            rank integer, shown text, evidence jsonb
        )
        """
    )
    with conn.cursor().copy(f"COPY {TMP} ({', '.join(_COLUMNS)}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(
                (
                    row.notice_id,
                    row.award_key,
                    row.piid,
                    row.method,
                    round(row.score, 4),
                    row.rank,
                    row.shown,
                    Jsonb(row.evidence),
                )
            )
    written = conn.execute(
        f"""
        INSERT INTO notice_award_matches
            (notice_id, award_key, piid, kind, method, score, rank, shown, evidence,
             matcher_version)
        SELECT notice_id, award_key, piid, 'incumbent', method, score, rank, shown, evidence,
               %(version)s
        FROM {TMP}
        ON CONFLICT (notice_id, award_key, kind) DO UPDATE SET
            piid = excluded.piid, method = excluded.method, score = excluded.score,
            rank = excluded.rank, shown = excluded.shown, evidence = excluded.evidence,
            matcher_version = excluded.matcher_version, updated_at = now()
        WHERE notice_award_matches.status = 'auto'
          AND (notice_award_matches.piid, notice_award_matches.method,
               notice_award_matches.score, notice_award_matches.rank,
               notice_award_matches.shown, notice_award_matches.evidence,
               notice_award_matches.matcher_version)
              IS DISTINCT FROM (excluded.piid, excluded.method, excluded.score,
                                excluded.rank, excluded.shown, excluded.evidence,
                                excluded.matcher_version)
        """,
        {"version": matcher_version},
    ).rowcount
    deleted = conn.execute(
        f"""
        DELETE FROM notice_award_matches m
        WHERE m.kind = 'incumbent' AND m.status = 'auto' AND m.notice_id = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM {TMP} t WHERE t.notice_id = m.notice_id AND t.award_key = m.award_key
          )
        """,
        (list(notice_ids),),
    ).rowcount
    conn.execute(f"DROP TABLE {TMP}")
    return written, deleted
