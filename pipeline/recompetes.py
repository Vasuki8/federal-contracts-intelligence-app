"""`app recompetes refresh`: vertical awards whose last possible end date is 6-24 months
away, with the incumbent, agency, value, set-aside, offers and any linked notice.

The table is rebuilt in one transaction (TRUNCATE frees the old rows' space at once)."""

from collections.abc import Iterable
from datetime import date

from pipeline.match.candidates import END_SQL, VALUE_SQL, Conn
from pipeline.match.config import MatchingConfig
from pipeline.match.features import add_months

SOURCE = "recompetes"
MONTHS_FROM = 6
MONTHS_TO = 24


def refresh_recompetes(
    conn: Conn, vertical: Iterable[str], cfg: MatchingConfig, today: date
) -> int:
    """Rebuild `recompetes`; the caller commits. Returns the number of rows."""
    conn.execute("TRUNCATE recompetes")
    return conn.execute(
        f"""
        INSERT INTO recompetes (
            award_key, piid, award_type_code, recipient_uei, recipient_name,
            awarding_agency_code, awarding_agency_name, awarding_sub_agency_code,
            awarding_sub_agency_name, awarding_office_code, awarding_office_name, naics, psc,
            set_aside_code, set_aside, number_of_offers, total_value, obligated_total,
            ultimate_end, linked_notice_id
        )
        SELECT a.award_key, a.piid, a.award_type_code, a.recipient_uei,
               coalesce(a.recipient_name, e.name), a.awarding_agency_code,
               a.awarding_agency_name, a.awarding_sub_agency_code, a.awarding_sub_agency_name,
               a.awarding_office_code, a.awarding_office_name, a.naics, a.psc,
               a.set_aside_code, a.set_aside, a.number_of_offers, {VALUE_SQL},
               a.obligated_total, {END_SQL}, link.notice_id
        FROM awards a
        LEFT JOIN entities e ON e.uei = a.recipient_uei
        LEFT JOIN LATERAL (
            SELECT m.notice_id FROM notice_award_matches m
            JOIN notices n ON n.notice_id = m.notice_id
            WHERE m.award_key = a.award_key AND m.kind = 'incumbent'
              AND m.shown = 'incumbent' AND m.status <> 'rejected' AND n.active
            ORDER BY n.posted_at DESC NULLS LAST, n.notice_id
            LIMIT 1
        ) link ON true
        WHERE a.naics = ANY(%(vertical)s)
          AND a.award_type_code = ANY(%(types)s)
          AND coalesce({VALUE_SQL}, 0) >= %(min_value)s
          AND {END_SQL} BETWEEN %(low)s AND %(high)s
        """,
        {
            "vertical": sorted(vertical),
            "types": list(cfg.candidates.award_types),
            "min_value": cfg.candidates.min_total_value,
            "low": add_months(today, MONTHS_FROM),
            "high": add_months(today, MONTHS_TO),
        },
    ).rowcount
