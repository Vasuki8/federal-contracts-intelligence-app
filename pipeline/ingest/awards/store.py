"""Load award rows: COPY into a temp table, then set-based upserts of reference data,
entities and awards. Newer `last_modified_date` wins, so overlapping chunks are safe.
Rows whose content is unchanged are not rewritten: a reload costs no storage."""

from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl
import psycopg
from psycopg.rows import TupleRow

from pipeline.ingest.awards.parser import ALL_COLUMNS, BUSINESS_TYPE_COLUMNS

TMP = "tmp_award_rows"


def _text(col: str) -> str:
    return f"nullif(btrim(t.{col}), '')"


def _cast(col: str, sql_type: str) -> str:
    """NULL instead of an error when a value doesn't parse (PostgreSQL 16+)."""
    value = _text(col)
    return f"(CASE WHEN pg_input_is_valid({value}, '{sql_type}') THEN {value}::{sql_type} END)"


def _flag(col: str) -> str:
    value = f"lower({_text(col)})"
    return (
        f"(CASE WHEN {value} IN ('t', 'true', 'y', 'yes', '1') THEN true "
        f"WHEN {value} IN ('f', 'false', 'n', 'no', '0') THEN false END)"
    )


def _business_types() -> str:
    pairs = []
    for col in BUSINESS_TYPE_COLUMNS:
        value = _text(col) if col.startswith("contracting_officers") else _flag(col)
        pairs.append(f"'{col}', {value}")
    return f"jsonb_strip_nulls(jsonb_build_object({', '.join(pairs)}))"


@dataclass
class LoadResult:
    rows_in: int = 0
    awards_upserted: int = 0
    entities_upserted: int = 0


def load_awards(
    conn: psycopg.Connection[TupleRow],
    frame: pl.DataFrame,
    raw_file_id: int | None,
    vertical: Iterable[str],
) -> LoadResult:
    """Upsert one CSV's rows. The caller commits (together with its chunk checkpoint)."""
    result = LoadResult(rows_in=frame.height)
    if frame.height == 0:
        return result
    _copy_to_temp(conn, frame)
    _upsert_reference_data(conn, sorted(vertical))
    result.entities_upserted = _upsert_entities(conn)
    result.awards_upserted = _upsert_awards(conn, raw_file_id)
    conn.execute(f"DROP TABLE {TMP}")
    return result


def seed_vertical_naics(conn: psycopg.Connection[TupleRow], vertical: Iterable[str]) -> None:
    conn.execute(
        """
        INSERT INTO naics (code, in_vertical) SELECT unnest(%s::text[]), true
        ON CONFLICT (code) DO UPDATE SET in_vertical = true, updated_at = now()
        """,
        (sorted(vertical),),
    )


def _copy_to_temp(conn: psycopg.Connection[TupleRow], frame: pl.DataFrame) -> None:
    columns = ", ".join(f"{c} text" for c in ALL_COLUMNS)
    conn.execute(f"DROP TABLE IF EXISTS {TMP}")
    conn.execute(f"CREATE TEMP TABLE {TMP} ({columns})")
    with conn.cursor().copy(f"COPY {TMP} ({', '.join(ALL_COLUMNS)}) FROM STDIN") as copy:
        for row in frame.select(ALL_COLUMNS).iter_rows():
            copy.write_row(row)


def _upsert_reference_data(conn: psycopg.Connection[TupleRow], vertical: list[str]) -> None:
    conn.execute(f"""
        INSERT INTO agencies (level, code, name)
        SELECT DISTINCT ON (code) 'department', code, name FROM (
            SELECT {_text("awarding_agency_code")} AS code, {_text("awarding_agency_name")} AS name
            FROM {TMP} t) s
        WHERE code IS NOT NULL ORDER BY code, name NULLS LAST
        ON CONFLICT (level, code) DO UPDATE SET
            name = coalesce(excluded.name, agencies.name), updated_at = now()
    """)
    conn.execute(f"""
        INSERT INTO agencies (level, code, name, parent_code)
        SELECT DISTINCT ON (code) 'subtier', code, name, parent FROM (
            SELECT {_text("awarding_sub_agency_code")} AS code,
                   {_text("awarding_sub_agency_name")} AS name,
                   {_text("awarding_agency_code")} AS parent
            FROM {TMP} t) s
        WHERE code IS NOT NULL ORDER BY code, name NULLS LAST
        ON CONFLICT (level, code) DO UPDATE SET
            name = coalesce(excluded.name, agencies.name),
            parent_code = coalesce(excluded.parent_code, agencies.parent_code),
            updated_at = now()
    """)
    conn.execute(f"""
        INSERT INTO offices (subtier_code, office_code, name)
        SELECT DISTINCT ON (subtier, office) subtier, office, name FROM (
            SELECT {_text("awarding_sub_agency_code")} AS subtier,
                   {_text("awarding_office_code")} AS office,
                   {_text("awarding_office_name")} AS name
            FROM {TMP} t) s
        WHERE subtier IS NOT NULL AND office IS NOT NULL
        ORDER BY subtier, office, name NULLS LAST
        ON CONFLICT (subtier_code, office_code) DO UPDATE SET
            name = coalesce(excluded.name, offices.name), updated_at = now()
    """)
    conn.execute(
        f"""
        INSERT INTO naics (code, title, in_vertical)
        SELECT DISTINCT ON (code) code, title, code = ANY(%s::text[]) FROM (
            SELECT {_text("naics_code")} AS code, {_text("naics_description")} AS title
            FROM {TMP} t) s
        WHERE code ~ '^[0-9]{{2,6}}$' ORDER BY code, title NULLS LAST
        ON CONFLICT (code) DO UPDATE SET
            title = coalesce(excluded.title, naics.title),
            in_vertical = naics.in_vertical OR excluded.in_vertical,
            updated_at = now()
        """,
        (vertical,),
    )


def _changed(table: str, columns: Iterable[str]) -> str:
    """Upsert condition: some column differs between the stored row and the incoming one."""
    names = list(columns)
    stored = ", ".join(f"{table}.{c}" for c in names)
    incoming = ", ".join(f"excluded.{c}" for c in names)
    return f"ROW({stored}) IS DISTINCT FROM ROW({incoming})"


ENTITY_COLUMNS = (
    "name",
    "parent_uei",
    "parent_name",
    "cage",
    "state",
    "business_types",
    "last_modified",
)


def _upsert_entities(conn: psycopg.Connection[TupleRow]) -> int:
    uei = _text("recipient_uei")
    modified = _cast("last_modified_date", "timestamptz")
    updates = ", ".join(f"{c} = excluded.{c}" for c in ENTITY_COLUMNS)
    cursor = conn.execute(f"""
        INSERT INTO entities (uei, {", ".join(ENTITY_COLUMNS)})
        SELECT DISTINCT ON ({uei})
            {uei}, {_text("recipient_name")}, {_text("recipient_parent_uei")},
            {_text("recipient_parent_name")}, {_text("cage_code")},
            {_text("recipient_state_code")}, {_business_types()}, {modified}
        FROM {TMP} t
        WHERE {uei} IS NOT NULL
        ORDER BY {uei}, {modified} DESC NULLS LAST
        ON CONFLICT (uei) DO UPDATE SET {updates}, updated_at = now()
        WHERE (entities.last_modified IS NULL
               OR excluded.last_modified >= entities.last_modified)
          AND {_changed("entities", ENTITY_COLUMNS)}
    """)
    return cursor.rowcount


def _upsert_awards(conn: psycopg.Connection[TupleRow], raw_file_id: int | None) -> int:
    targets = {
        "award_key": _text("contract_award_unique_key"),
        "piid": _text("award_id_piid"),
        "referenced_idv_piid": _text("parent_award_id_piid"),
        "award_type_code": _text("award_type_code"),
        "award_type": _text("award_type"),
        "idv_type_code": _text("idv_type_code"),
        "awarding_agency_code": _text("awarding_agency_code"),
        "awarding_agency_name": _text("awarding_agency_name"),
        "awarding_sub_agency_code": _text("awarding_sub_agency_code"),
        "awarding_sub_agency_name": _text("awarding_sub_agency_name"),
        "awarding_office_code": _text("awarding_office_code"),
        "awarding_office_name": _text("awarding_office_name"),
        "office_id": "o.id",
        "recipient_uei": _text("recipient_uei"),
        "recipient_name": _text("recipient_name"),
        "naics": _text("naics_code"),
        "psc": _text("product_or_service_code"),
        "solicitation_id": _text("solicitation_identifier"),
        "set_aside_code": _text("type_of_set_aside_code"),
        "set_aside": _text("type_of_set_aside"),
        "extent_competed_code": _text("extent_competed_code"),
        "number_of_offers": _cast("number_of_offers_received", "integer"),
        "obligated_total": _cast("total_obligated_amount", "numeric"),
        "current_total_value": _cast("current_total_value_of_award", "numeric"),
        "total_value": _cast("potential_total_value_of_award", "numeric"),
        "pop_start": _cast("period_of_performance_start_date", "date"),
        "current_end": _cast("period_of_performance_current_end_date", "date"),
        "ultimate_end": _cast("period_of_performance_potential_end_date", "date"),
        "ordering_period_end": _cast("ordering_period_end_date", "date"),
        "base_action_date": _cast("award_base_action_date", "date"),
        "latest_action_date": _cast("award_latest_action_date", "date"),
        "description": _text("prime_award_base_transaction_description"),
        "last_modified": _cast("last_modified_date", "timestamptz"),
        "usaspending_url": _text("usaspending_permalink"),
    }
    columns = ", ".join([*targets, "raw_file_id"])
    selects = ", ".join(f"{expr} AS {name}" for name, expr in targets.items())
    content = [name for name in targets if name != "award_key"]
    updates = ", ".join(f"{name} = excluded.{name}" for name in [*content, "raw_file_id"])
    cursor = conn.execute(
        f"""
        INSERT INTO awards ({columns})
        SELECT DISTINCT ON (award_key) * FROM (
            SELECT {selects}, %s::bigint AS raw_file_id
            FROM {TMP} t
            LEFT JOIN offices o
              ON o.subtier_code = {_text("awarding_sub_agency_code")}
             AND o.office_code = {_text("awarding_office_code")}
        ) s
        WHERE award_key IS NOT NULL
        ORDER BY award_key, last_modified DESC NULLS LAST
        ON CONFLICT (award_key) DO UPDATE SET {updates}, updated_at = now()
        WHERE (awards.last_modified IS NULL OR excluded.last_modified >= awards.last_modified)
          AND {_changed("awards", content)}
        """,
        (raw_file_id,),
    )
    return cursor.rowcount
