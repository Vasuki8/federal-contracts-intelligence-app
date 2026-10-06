# Test fixtures: where each file came from

Tests never call the network. Every fixture below is one of three kinds:
- **docs**: copied verbatim from the official API documentation.
- **synthetic**: built by hand in the documented shape. Values are invented and clearly fake.
- **live**: a real response saved by `uv run app ingest sample-fixtures`.

Synthetic fixtures stand in until live ones exist. When a live sample disagrees with them,
the live sample wins: fix the parser, then the synthetic fixture.

| File | Kind | Source |
|---|---|---|
| `sam_opportunities/docs_example_1.json` | docs | GSA/open-gsa-redesign@cc568a8, `_apidocs/get-opportunities-public-api.md`, "Example 1" response, verbatim |
| `sam_opportunities/docs_example_2.json` | docs + wrapper | Same file, "Example 2" record verbatim. The docs show only the record, so it is wrapped in Example 1's page envelope. |
| `sam_opportunities/amendment_v1.json`, `amendment_v2.json` | synthetic | Example 2's shape with vertical NAICS 541512, a set-aside and attachments. v2 changes the title, deadline, posted date and adds an attachment. URLs use `example.invalid`. The deadline's ISO-with-offset format is an assumption. |
| `usaspending/docs_download_awards_response.json` | docs | fedspendingtransparency/usaspending-api@03b9e25, `api_contracts/contracts/v2/download/awards.md`, response body, verbatim |
| `usaspending/docs_download_status_finished.json` | docs | Same repo, `download/status.md`, response body, verbatim |
| `usaspending/award_d1_columns.txt` | docs (code) | The 286 award-level contract download columns, in order: `query_paths["award"]["d1"]` in `usaspending_api/download/v2/download_column_historical_lookups.py`, read with Python's `ast` (not executed) |
| `usaspending/contracts_prime_award_summaries_*.csv` | synthetic | Real 286-column header. Invented rows: an IDV, a task order under it, a definitive contract (with deliberately malformed numbers and dates), and an older copy of the task order. `t`/`f` flags match live data; live `last_modified_date` also carries `+00`, and both forms parse. |
| `sam_opportunities/live_sample.json` | live | Real `/opportunities/v2/search` page (limit 3, posted 2026-09-29..2026-10-06), fetched 2026-10-06 by the `Live sample` GitHub Actions workflow |
| `usaspending/live_sample.csv` | live | Real award-level CSV from `/api/v2/download/awards/` (NAICS 541512, action date in the last 30 days, limit 25), same run |
