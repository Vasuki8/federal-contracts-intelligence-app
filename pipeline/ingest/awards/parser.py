"""Read award-level contract CSVs from a USAspending download zip.

Column names are the "award"/"d1" download columns defined in USAspending's
`download/v2/download_column_historical_lookups.py` (see docs/data-sources.md).
Everything is read as text; typed casts happen in SQL (`pg_input_is_valid`), so a
malformed value becomes NULL instead of failing the whole file.
"""

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import polars as pl

KEY_COLUMN = "contract_award_unique_key"

AWARD_COLUMNS: tuple[str, ...] = (
    KEY_COLUMN,
    "award_id_piid",
    "parent_award_id_piid",
    "award_type_code",
    "award_type",
    "idv_type_code",
    "awarding_agency_code",
    "awarding_agency_name",
    "awarding_sub_agency_code",
    "awarding_sub_agency_name",
    "awarding_office_code",
    "awarding_office_name",
    "recipient_uei",
    "recipient_name",
    "recipient_parent_uei",
    "recipient_parent_name",
    "cage_code",
    "recipient_state_code",
    "naics_code",
    "naics_description",
    "product_or_service_code",
    "solicitation_identifier",
    "type_of_set_aside_code",
    "type_of_set_aside",
    "extent_competed_code",
    "number_of_offers_received",
    "total_obligated_amount",
    "current_total_value_of_award",
    "potential_total_value_of_award",
    "period_of_performance_start_date",
    "period_of_performance_current_end_date",
    "period_of_performance_potential_end_date",
    "ordering_period_end_date",
    "award_base_action_date",
    "award_latest_action_date",
    "prime_award_base_transaction_description",
    "last_modified_date",
    "usaspending_permalink",
)

# Recipient business-type flags kept on `entities.business_types` (certification signals).
BUSINESS_TYPE_COLUMNS: tuple[str, ...] = (
    "c8a_program_participant",
    "sba_certified_8a_joint_venture",
    "historically_underutilized_business_zone_hubzone_firm",
    "service_disabled_veteran_owned_business",
    "veteran_owned_business",
    "women_owned_small_business",
    "economically_disadvantaged_women_owned_small_business",
    "woman_owned_business",
    "small_disadvantaged_business",
    "self_certified_small_disadvantaged_business",
    "contracting_officers_determination_of_business_size",
)

ALL_COLUMNS: tuple[str, ...] = AWARD_COLUMNS + BUSINESS_TYPE_COLUMNS


class NotAnAwardFile(ValueError):
    pass


@dataclass
class AwardFrame:
    """Rows from one CSV, every column in ALL_COLUMNS present (missing ones as nulls)."""

    frame: pl.DataFrame
    member: str
    missing_columns: tuple[str, ...]


def csv_header(path: Path) -> list[str]:
    return pl.read_csv(path, n_rows=0, infer_schema=False).columns


def read_award_csv(path: Path, member: str = "") -> AwardFrame:
    header = csv_header(path)
    if KEY_COLUMN not in header:
        raise NotAnAwardFile(f"{member or path.name} has no {KEY_COLUMN} column")
    present = [c for c in ALL_COLUMNS if c in header]
    frame = (
        pl.scan_csv(path, infer_schema=False)
        .select(present)
        .with_columns(
            pl.lit(None, dtype=pl.String).alias(c) for c in ALL_COLUMNS if c not in header
        )
        .select(ALL_COLUMNS)
        .collect()
    )
    missing = tuple(c for c in ALL_COLUMNS if c not in header)
    return AwardFrame(frame=frame, member=member or path.name, missing_columns=missing)


def read_award_zip(zip_path: Path, work_dir: Path) -> list[AwardFrame]:
    """Every award-level CSV in a download zip (large downloads are split into parts)."""
    frames = []
    work_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            if not name.lower().endswith(".csv"):
                continue
            target = work_dir / Path(name).name
            with archive.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                frames.append(read_award_csv(target, name))
            except NotAnAwardFile:
                pass
            finally:
                target.unlink(missing_ok=True)
    return frames
