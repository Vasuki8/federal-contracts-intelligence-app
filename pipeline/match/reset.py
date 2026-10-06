"""`app match reset`: remove every automatic match and give the space back to the database.

Use it when matches must be rebuilt from scratch, e.g. after a failed run left a bloated
table behind (rolled-back rows still take space until the table is rewritten). Human
decisions survive: with none recorded the tables are truncated, which frees the space at
once even on a database that is over its size limit; otherwise automatic rows are
deleted and the table is rewritten with VACUUM FULL. The next `app match notices`
recomputes everything."""

from pipeline.match.candidates import Conn


def reset_automatic_matches(conn: Conn) -> str:
    """`conn` must be in autocommit mode (VACUUM can't run inside a transaction)."""
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM notice_award_matches WHERE status <> 'auto'),
               (SELECT count(*) FROM match_reviews)
        """
    ).fetchone()
    human, reviews = (int(row[0]), int(row[1])) if row else (0, 0)
    if human == 0 and reviews == 0:
        conn.execute("TRUNCATE notice_award_matches, match_reviews, recompetes")
        return "Removed all matches (no human decisions existed) and freed their space."
    deleted = conn.execute("DELETE FROM notice_award_matches WHERE status = 'auto'").rowcount
    conn.execute("TRUNCATE recompetes")
    conn.execute("VACUUM (FULL, ANALYZE) notice_award_matches")
    return (
        f"Removed {deleted:,} automatic matches, kept {human:,} human decisions and "
        f"{reviews:,} reviews, and rewrote the table to free the space."
    )
