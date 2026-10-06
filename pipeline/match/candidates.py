"""Database reads for the matcher: notices in scope, candidate awards and run context."""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from pipeline.match.config import MatchingConfig
from pipeline.match.features import AwardFacts, NoticeFacts, candidate_window

Conn = psycopg.Connection[TupleRow]

# Same normalization as the generated *_norm columns (migration 0002).
NORM_SQL = "nullif(upper(regexp_replace(coalesce({col}, ''), '[[:space:]-]+', '', 'g')), '')"
END_SQL = "coalesce(a.ultimate_end, a.ordering_period_end)"
VALUE_SQL = "coalesce(a.total_value, a.current_total_value, a.obligated_total)"

_AWARD_COLUMNS = f"""
    a.award_key, a.piid, a.piid_norm, a.award_type_code, a.awarding_sub_agency_code,
    a.awarding_sub_agency_name, a.awarding_office_code, a.awarding_office_name, a.naics,
    a.psc, a.set_aside_code, a.set_aside, {VALUE_SQL}::float8, {END_SQL}, a.description,
    a.recipient_uei, a.recipient_name, {NORM_SQL.format(col="a.referenced_idv_piid")}
"""


def _award(row: Sequence[Any]) -> AwardFacts:
    return AwardFacts(*row)


def reference_date(
    deadline: datetime | None, posted: datetime | None, cfg: MatchingConfig, today: date
) -> date:
    if deadline is not None:
        return deadline.date()
    if posted is not None:
        return posted.date() + timedelta(days=cfg.days_after_posting_without_deadline)
    return today


def load_notices(
    conn: Conn, vertical: Iterable[str], cfg: MatchingConfig, today: date
) -> list[NoticeFacts]:
    """Active vertical notices of a type that can have an incumbent and not yet past their
    archive date, with the latest description text when one has been loaded."""
    rows = conn.execute(
        """
        SELECT n.notice_id, n.title, d.text, n.solicitation_number, n.subtier_code,
               n.office_code, n.naics, n.naics_codes, n.psc, n.set_aside_code,
               n.response_deadline, n.posted_at
        FROM notices n
        LEFT JOIN LATERAL (
            SELECT text FROM notice_descriptions nd
            WHERE nd.notice_id = n.notice_id ORDER BY nd.version DESC LIMIT 1
        ) d ON true
        WHERE n.active AND n.type = ANY(%(types)s)
          AND (n.archive_date IS NULL OR n.archive_date >= %(today)s)
          AND (n.naics = ANY(%(vertical)s) OR n.naics_codes && %(vertical)s)
        ORDER BY n.notice_id
        """,
        {"types": list(cfg.notice_types), "vertical": sorted(vertical), "today": today},
    ).fetchall()
    notices = []
    for row in rows:
        (notice_id, title, text, sol, subtier, office, naics, codes, psc, set_aside) = row[:10]
        deadline, posted = row[10], row[11]
        notices.append(
            NoticeFacts(
                notice_id=notice_id,
                title=title,
                description=text,
                solicitation_number=sol,
                subtier_code=subtier,
                office_code=office,
                naics_codes=tuple(dict.fromkeys(c for c in (naics, *(codes or ())) if c)),
                psc=psc,
                set_aside_code=set_aside,
                reference_date=reference_date(deadline, posted, cfg, today),
                has_deadline=deadline is not None,
            )
        )
    return notices


def fetch_candidates(
    conn: Conn, notice: NoticeFacts, cfg: MatchingConfig, exclude: Iterable[str] = ()
) -> list[AwardFacts]:
    """Same sub-tier, same office or related NAICS/PSC, ending inside the window."""
    if notice.subtier_code is None:
        return []
    low, high = candidate_window(notice.reference_date, cfg.candidates)
    c = cfg.candidates
    rows = conn.execute(
        f"""
        SELECT {_AWARD_COLUMNS} FROM awards a
        WHERE a.awarding_sub_agency_code = %(subtier)s
          AND (a.ultimate_end BETWEEN %(low)s AND %(high)s
               OR (a.ultimate_end IS NULL AND a.ordering_period_end BETWEEN %(low)s AND %(high)s))
          AND a.award_type_code = ANY(%(types)s)
          AND coalesce({VALUE_SQL}, 0) >= %(min_value)s
          AND (a.awarding_office_code = %(office)s
               OR left(a.naics, 4) = ANY(%(naics4)s)
               OR left(a.psc, 2) = %(psc2)s)
          AND NOT (a.award_key = ANY(%(exclude)s))
        ORDER BY (a.awarding_office_code IS NOT DISTINCT FROM %(office)s) DESC,
                 abs({END_SQL} - %(ref)s::date), a.award_key
        LIMIT %(limit)s
        """,
        {
            "subtier": notice.subtier_code,
            "low": low,
            "high": high,
            "types": list(c.award_types),
            "min_value": c.min_total_value,
            "office": notice.office_code,
            "naics4": sorted({code[:4] for code in notice.naics_codes}),
            "psc2": notice.psc[:2] if notice.psc else None,
            "exclude": sorted(exclude),
            "ref": notice.reference_date,
            "limit": c.max_per_notice,
        },
    ).fetchall()
    return [_award(row) for row in rows]


def awards_by_piid(conn: Conn, piid_norms: Iterable[str]) -> list[AwardFacts]:
    keys = sorted(set(piid_norms))
    if not keys:
        return []
    rows = conn.execute(
        f"SELECT {_AWARD_COLUMNS} FROM awards a WHERE a.piid_norm = ANY(%s) ORDER BY a.award_key",
        (keys,),
    ).fetchall()
    return [_award(row) for row in rows]


def superseded_awards(conn: Conn) -> dict[str, set[str]]:
    """award_key → notices that already replaced it: the award is that notice's incumbent
    and the notice has since been awarded."""
    rows = conn.execute(
        """
        SELECT m.award_key, m.notice_id FROM notice_award_matches m
        WHERE m.kind = 'incumbent'
          AND (m.status = 'confirmed' OR (m.status = 'auto' AND m.shown = 'incumbent'))
          AND EXISTS (
              SELECT 1 FROM notice_award_matches r
              WHERE r.notice_id = m.notice_id AND r.kind = 'resulting_award'
                AND r.status <> 'rejected'
          )
        """
    ).fetchall()
    result: dict[str, set[str]] = defaultdict(set)
    for award_key, notice_id in rows:
        result[award_key].add(notice_id)
    return dict(result)


def resulting_awards(conn: Conn) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        SELECT notice_id, award_key FROM notice_award_matches
        WHERE kind = 'resulting_award' AND status <> 'rejected'
        """
    ).fetchall()
    result: dict[str, set[str]] = defaultdict(set)
    for notice_id, award_key in rows:
        result[notice_id].add(award_key)
    return dict(result)


def human_reviews(conn: Conn) -> dict[str, dict[str, str]]:
    """notice_id → {award_key: 'confirmed' | 'rejected'} for incumbent matches."""
    rows = conn.execute(
        """
        SELECT notice_id, award_key, status FROM notice_award_matches
        WHERE kind = 'incumbent' AND status <> 'auto'
        """
    ).fetchall()
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for notice_id, award_key, status in rows:
        result[notice_id][award_key] = status
    return dict(result)


def award_descriptions(conn: Conn, vertical: Iterable[str]) -> Iterator[str | None]:
    """Every vertical award description, streamed (the TF-IDF corpus)."""
    with conn.cursor(name="matcher_award_text") as cursor:
        cursor.itersize = 10_000
        cursor.execute("SELECT description FROM awards WHERE naics = ANY(%s)", (sorted(vertical),))
        for (description,) in cursor:
            yield description
