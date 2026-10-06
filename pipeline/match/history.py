"""History links: the award made *from* a notice (not its incumbent).

A notice's solicitation number matching an award's solicitation id within the same
sub-tier agency (solicitation numbers repeat across agencies), or an award notice whose
`award.number` (or, for orders, its solicitation number) is the award's PIID. These
label past notices with their winner, feed the evaluation, and mark incumbents that
have already been replaced.

Only numbers shaped like real contract numbers are matched (8-20 letters and digits,
with a letter and at least 4 digits). Placeholders such as "N/A" or "TBD" are shared by
thousands of awards in an agency, and the first hosted run filled the database with
such links. A notice that still links to more than MAX_LINKS_PER_NOTICE awards is
skipped: that many is not "the award made from it"."""

from collections.abc import Iterable

from psycopg.types.json import Jsonb

from pipeline.match.candidates import NORM_SQL, Conn

REASONS = {
    "same_solicitation": "Awarded under this notice's solicitation number",
    "award_notice_number": "The award notice names this contract number",
}
TMP = "tmp_history_links"
MAX_LINKS_PER_NOTICE = 50
# Same rule as piids.looks_like_contract_number, on an already normalized value.
LOOKS_LIKE_ID = (
    "({col} ~ '^[A-Z0-9]{{8,20}}$' AND {col} ~ '[A-Z]' AND {col} ~ '([0-9][^0-9]*){{4}}')"
)


def link_history(conn: Conn, vertical: Iterable[str], matcher_version: str) -> tuple[int, int]:
    """Upsert `resulting_award` links for every vertical notice; delete automatic ones that
    no longer hold. Returns (written, deleted)."""
    codes = sorted(vertical)
    award_number = NORM_SQL.format(col="n.award ->> 'number'")
    solicitation_ok = LOOKS_LIKE_ID.format(col="n.solicitation_number_norm")
    award_number_ok = LOOKS_LIKE_ID.format(col=award_number)
    conn.execute(f"DROP TABLE IF EXISTS {TMP}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {TMP} AS
        SELECT notice_id, award_key, piid, method FROM (
            SELECT DISTINCT ON (notice_id, award_key) notice_id, award_key, piid, method,
                   count(*) OVER (PARTITION BY notice_id) AS links
            FROM (
                SELECT n.notice_id, a.award_key, a.piid, 'same_solicitation' AS method
                FROM notices n
                JOIN awards a ON a.solicitation_id_norm = n.solicitation_number_norm
                             AND a.awarding_sub_agency_code = n.subtier_code
                WHERE {solicitation_ok}
                  AND (n.naics = ANY(%(codes)s) OR n.naics_codes && %(codes)s)
                UNION ALL
                SELECT n.notice_id, a.award_key, a.piid, 'award_notice_number'
                FROM notices n
                JOIN awards a ON a.piid_norm = {award_number}
                             AND a.awarding_sub_agency_code = n.subtier_code
                WHERE n.award ? 'number' AND {award_number_ok}
                  AND (n.naics = ANY(%(codes)s) OR n.naics_codes && %(codes)s)
                UNION ALL
                -- Award notices for orders often carry the order number as the
                -- solicitation number and the contract vehicle as the award number.
                SELECT n.notice_id, a.award_key, a.piid, 'award_notice_number'
                FROM notices n
                JOIN awards a ON a.piid_norm = n.solicitation_number_norm
                             AND a.awarding_sub_agency_code = n.subtier_code
                WHERE n.type = 'Award Notice' AND {solicitation_ok}
                  AND (n.naics = ANY(%(codes)s) OR n.naics_codes && %(codes)s)
            ) found
            ORDER BY notice_id, award_key, method
        ) links
        WHERE links <= %(max_links)s
        """,
        {"codes": codes, "max_links": MAX_LINKS_PER_NOTICE},
    )
    written = 0
    for method, reason in REASONS.items():
        cursor = conn.execute(
            f"""
            INSERT INTO notice_award_matches
                (notice_id, award_key, piid, kind, method, score, shown, evidence,
                 matcher_version)
            SELECT notice_id, award_key, piid, 'resulting_award', method, 1, 'hidden',
                   %(evidence)s, %(version)s
            FROM {TMP} WHERE method = %(method)s
            ON CONFLICT (notice_id, award_key, kind) DO UPDATE SET
                method = excluded.method, evidence = excluded.evidence,
                matcher_version = excluded.matcher_version, updated_at = now()
            WHERE notice_award_matches.status = 'auto'
              AND (notice_award_matches.method, notice_award_matches.evidence,
                   notice_award_matches.matcher_version)
                  IS DISTINCT FROM (excluded.method, excluded.evidence, excluded.matcher_version)
            """,
            {
                "evidence": Jsonb({"reasons": [reason]}),
                "version": matcher_version,
                "method": method,
            },
        )
        written += cursor.rowcount
    deleted = conn.execute(
        f"""
        DELETE FROM notice_award_matches m
        WHERE m.kind = 'resulting_award' AND m.status = 'auto'
          AND NOT EXISTS (
              SELECT 1 FROM {TMP} t WHERE t.notice_id = m.notice_id AND t.award_key = m.award_key
          )
        """
    ).rowcount
    conn.execute(f"DROP TABLE {TMP}")
    return written, deleted
