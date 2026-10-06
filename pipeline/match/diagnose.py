"""`app match diagnose`: checks that tell whether the matcher's inputs can be trusted.

- Office codes: do notice offices use the same codes as award offices?
- Coverage: how many notices we hold, of which types, whether archived ones are present,
  and what SAM.gov reported as `totalRecords` for each backfill chunk.
- Text: how many notices have description text (the strongest evidence lives there).
- Results: what the matcher currently shows."""

from collections.abc import Iterable

from pipeline.match.candidates import Conn
from pipeline.match.config import MatchingConfig

VERTICAL_SQL = "(n.naics = ANY(%(vertical)s) OR n.naics_codes && %(vertical)s)"


def _pct(part: int, whole: int) -> str:
    return f"{part:,} of {whole:,} ({100 * part / whole:.0f}%)" if whole else "0 of 0"


def diagnose(conn: Conn, vertical: Iterable[str], cfg: MatchingConfig) -> list[str]:
    params = {"vertical": sorted(vertical), "types": list(cfg.notice_types)}
    lines = ["Notices in the vertical"]
    rows = conn.execute(
        f"""
        SELECT coalesce(n.type, '(none)'), count(*) FILTER (WHERE n.active),
               count(*) FILTER (WHERE NOT n.active OR n.active IS NULL)
        FROM notices n WHERE {VERTICAL_SQL} GROUP BY 1 ORDER BY 2 DESC, 1
        """,
        params,
    ).fetchall()
    for kind, active, inactive in rows:
        lines.append(f"  {kind:<34}{active:>7,} active{inactive:>9,} archived")
    span = conn.execute(
        f"SELECT min(posted_at)::date, max(posted_at)::date FROM notices n WHERE {VERTICAL_SQL}",
        params,
    ).fetchone()
    if span and span[0]:
        lines.append(f"  posted between {span[0]} and {span[1]}")

    lines.append("Office codes (active notices that can have an incumbent)")
    office = conn.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM awards a WHERE a.awarding_sub_agency_code = n.subtier_code)),
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM awards a WHERE a.awarding_sub_agency_code = n.subtier_code
                     AND a.awarding_office_code = n.office_code))
        FROM notices n
        WHERE n.active AND n.type = ANY(%(types)s) AND {VERTICAL_SQL}
        """,
        params,
    ).fetchone()
    total, subtier, same_office = office if office else (0, 0, 0)
    lines.append(f"  sub-tier found in awards      {_pct(subtier, total)}")
    lines.append(f"  office found in awards        {_pct(same_office, total)}")

    lines.append("SAM.gov backfill chunks (totalRecords reported by SAM)")
    chunks = conn.execute(
        """
        SELECT chunk_key, status, (state ->> 'total_records')::int
        FROM ingest_chunks WHERE source = 'sam_opportunities' ORDER BY chunk_key
        """
    ).fetchall()
    for key, status, total_records in chunks:
        lines.append(f"  {key}: {total_records if total_records is not None else '?'} ({status})")
    if not chunks:
        lines.append("  none")

    lines.append("Description text")
    text = conn.execute(
        f"""
        SELECT count(*), count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM notice_descriptions d WHERE d.notice_id = n.notice_id
              AND coalesce(d.text, '') <> ''))
        FROM notices n WHERE n.active AND {VERTICAL_SQL}
        """,
        params,
    ).fetchone()
    lines.append(f"  active notices with a description  {_pct(text[1], text[0]) if text else '-'}")

    lines.append("Matcher results (active notices)")
    shown = conn.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM notice_award_matches m
                   WHERE m.notice_id = n.notice_id AND m.kind = 'incumbent'
                     AND m.shown = 'incumbent')),
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM notice_award_matches m
                   WHERE m.notice_id = n.notice_id AND m.kind = 'incumbent'
                     AND m.shown = 'possible')),
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM notice_award_matches m
                   WHERE m.notice_id = n.notice_id AND m.method = 'explicit_reference'))
        FROM notices n
        WHERE n.active AND n.type = ANY(%(types)s) AND {VERTICAL_SQL}
        """,
        params,
    ).fetchone()
    total, incumbent, possible, explicit = shown if shown else (0, 0, 0, 0)
    lines.append(f"  incumbent shown               {_pct(incumbent, total)}")
    lines.append(f"  possible incumbents only      {_pct(possible, total)}")
    lines.append(f"  cites a contract number       {_pct(explicit, total)}")
    links = conn.execute(
        "SELECT count(DISTINCT notice_id) FROM notice_award_matches WHERE kind = 'resulting_award'"
    ).fetchone()
    lines.append(f"  notices linked to the award made from them  {links[0] if links else 0:,}")
    return lines
