"""`app ingest sample-fixtures`: fetch small real responses and save them as test fixtures.

This is the first thing to run once the network allows the data hosts. It settles what
the docs leave unclear: actual field names, date formats, and how SAM's `offset` works.
"""

import json
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pipeline.ingest.awards.client import AwardsClient, download_filters
from pipeline.ingest.awards.parser import ALL_COLUMNS, csv_header
from pipeline.ingest.context import IngestContext
from pipeline.ingest.http import redact_text
from pipeline.ingest.opportunities.client import OpportunitiesClient
from pipeline.ingest.opportunities.parser import DOCUMENTED_FIELDS

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def offset_semantics(
    first: Sequence[Mapping[str, Any]], second: Sequence[Mapping[str, Any]]
) -> str:
    """Compare page offset=0 and offset=1 fetched with the same small limit."""
    if len(first) < 2 or not second:
        return "unknown (not enough records to tell)"
    if second[0].get("noticeId") == first[1].get("noticeId"):
        return "record offset (next page: offset + limit)"
    if {r.get("noticeId") for r in second}.isdisjoint({r.get("noticeId") for r in first}):
        return "page index (next page: offset + 1), as the docs say"
    return "unclear (pages overlap unexpectedly)"


def header_report(header: Sequence[str], documented: Sequence[str]) -> list[str]:
    lines = []
    missing = [c for c in ALL_COLUMNS if c not in header]
    if missing:
        lines.append(f"Columns the loader uses but the live file lacks: {missing}")
    extra = sorted(set(header) - set(documented))
    if extra:
        lines.append(f"Live columns not in the documented layout: {extra}")
    return lines or ["Live award header matches the documented layout."]


def sam_sample(ctx: IngestContext, client: OpportunitiesClient, api_key: str, today: date) -> None:
    start = today - timedelta(days=7)
    first = client.search(posted_from=start, posted_to=today, offset=0, limit=3)
    second = client.search(posted_from=start, posted_to=today, offset=1, limit=3)
    records = [r for r in first.get("opportunitiesData") or [] if isinstance(r, Mapping)]
    target = FIXTURES / "sam_opportunities" / "live_sample.json"
    target.write_text(redact_text(json.dumps(first, indent=4), (api_key,)) + "\n")
    ctx.log(f"Saved {target.relative_to(FIXTURES.parent.parent.parent)}")
    unknown = sorted({k for r in records for k in r} - DOCUMENTED_FIELDS)
    ctx.log(f"SAM fields not in the docs: {unknown or 'none'}")
    seen = sorted(
        {k for r in records for k in r}
        & {"responseDeadLine", "reponseDeadLine", "typeOfSetAside", "setAsideCode"}
    )
    ctx.log(f"SAM spellings in use: {seen}")
    second_records = [r for r in second.get("opportunitiesData") or [] if isinstance(r, Mapping)]
    ctx.log(f"SAM offset behaves as: {offset_semantics(records, second_records)}")


def usaspending_sample(ctx: IngestContext, client: AwardsClient, today: date) -> None:
    filters = download_filters(today - timedelta(days=30), today, ["541512"], "action_date")
    file_name = client.start(filters, limit=25)
    status = client.wait(file_name)
    download = client.fetch(status, {"filters": filters, "file_name": file_name})
    out_dir = FIXTURES / "usaspending"
    with zipfile.ZipFile(ctx.archive_root / download.file.path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not members:
            ctx.log("USAspending sample zip had no CSV files.")
            return
        target = out_dir / "live_sample.csv"
        target.write_bytes(archive.read(members[0]))
    ctx.log(f"Saved {target.relative_to(FIXTURES.parent.parent.parent)} ({members[0]})")
    documented = (out_dir / "award_d1_columns.txt").read_text().split()
    for line in header_report(csv_header(target), documented):
        ctx.log(line)
