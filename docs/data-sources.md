# Data sources

**How these were read.** The build container can't reach open.gsa.gov, sam.gov or
usaspending.gov. Both sites publish their API docs from public GitHub repositories, so
the docs were read there:

| Docs | Repository @ commit (date) |
|---|---|
| SAM.gov APIs (open.gsa.gov) | `GSA/open-gsa-redesign` @ `cc568a8` (2026-10-05), `_apidocs/*.md` |
| USAspending API contracts + download column definitions | `fedspendingtransparency/usaspending-api` @ `03b9e25` (2026-10-05) |

⚠ **verify live** marks anything still unclear. **Live sample (2026-10-06):** run in
GitHub Actions (`Live sample` workflow), saved as
`pipeline/tests/fixtures/sam_opportunities/live_sample.json` and
`pipeline/tests/fixtures/usaspending/live_sample.csv`. ✅ marks what it settled.

---

## 1. SAM.gov Get Opportunities API (notices)
Used by `app ingest opportunities` (backfill + daily delta).

| Item | Value |
|---|---|
| Docs | `_apidocs/get-opportunities-public-api.md` (open.gsa.gov/api/get-opportunities-public-api) |
| Auth | `api_key` query parameter. Personal key from SAM.gov → Account Details. Setting: `SAM_API_KEY`. |
| Endpoint | `GET https://api.sam.gov/opportunities/v2/search` (alpha: `https://api-alpha.sam.gov/opportunities/v2/search`) |
| Required params | `api_key`; `postedFrom` and `postedTo` (format `MM/dd/yyyy`, **at most 1 year apart**) |
| Params we use | `postedFrom`, `postedTo`, `limit`, `offset`, `ncode` (NAICS, "maximum of 6 digits") |
| Other params | `ptype`, `solnum`, `noticeid`, `title`, `state`, `zip`, `organizationCode`, `organizationName`, `typeOfSetAside`, `typeOfSetAsideDescription`, `ccode`, `rdlfrom`/`rdlto`, `status` ("Coming Soon"); `deptname`/`subtier` deprecated |
| Paging | `limit` max 1000 (default 1). `offset` is the **page index** (next page = offset + 1). ✅ live: confirmed. |
| Daily limit | "Request per day are limited based on the federal or non-federal or general roles." **No numbers on this page.** Other SAM APIs document 10/day (non-federal, no role) and 1,000/day (with a role, or federal). Our budget: `SAM_DAILY_REQUEST_LIMIT` (default 10), counted from `raw_files` since 00:00 UTC (⚠ verify the reset time). |
| Errors | 404 = "No Data found" (treated as an empty page); 400 bad request; 500 server error. Messages listed for bad limit, date format, >1-year range, missing or invalid key. |
| Versions | **"This API only provides the latest active version of the opportunity."** Older versions are only in SAM.gov Data Services extracts. |
| Description text | `description` is a link (`.../noticedesc?noticeid=...`) that needs the API key, so **one request per notice**. Not fetched in M1. |
| Response envelope | `totalRecords`, `limit`, `offset`, `opportunitiesData[]`, `links[]` |

**Record fields** (exact names). The field table and the example response disagree; the parser accepts both. ✅ Live responses use the **example** spellings:

| Field table says | Example response says | Stored as |
|---|---|---|
| `reponseDeadLine` (sic) | `responseDeadLine` | `notices.response_deadline` |
| `setAsideCode` / `setAside` | `typeOfSetAside` / `typeOfSetAsideDescription` | `set_aside_code` / `set_aside` |
| `pointofContact` | `pointOfContact` (with `fullName`; table says `fullname`) | `contacts` (stored as-is) |
| `officeAddress.zip` | `officeAddress.zipcode` | not stored (raw archive only) |

Agreed fields: `noticeId`, `title`, `solicitationNumber`, `fullParentPathName`,
`fullParentPathCode` (e.g. `047.4732.47QTCA` = department.sub-tier.office), `postedDate`
(table: `YYYY-MM-DD HH:MM:SS`; example: `2018-05-04`; both parsed), `type`, `baseType`,
`archiveType`, `archiveDate`, `naicsCode`, `classificationCode` (PSC), `active`
(`Yes`/`No`), `award` (`date`, `number`, `amount`, `awardee{name, location, ueiSAM}`),
`description`, `organizationType`, `placeOfPerformance{streetAddress, city{code,name},
state{code,name}, country{code,name}, zip}`, `additionalInfoLink`, `uiLink`, `links`,
`resourceLinks` (attachment URLs). Deprecated: `department`, `subTier`, `office`.

✅ Live findings (2026-10-06):
- `postedDate` and `responseDeadLine` arrived as plain dates (`2026-10-13`). Date-only
  deadlines are stored at 00:00 UTC with `notices.response_deadline_has_time = false`.
  Values with a time are parsed with their offset.
- **Undocumented `naicsCodes`** (list of strings) on every record, equal to `[naicsCode]`
  in the sample. Stored in `notices.naics_codes`; the vertical filter checks all of them.
- **`fullParentPathCode` has 2–5 levels**, e.g. `097.97AS.DLA LAND.DLA LAND COLUMBUS.SPE7L1`:
  department, sub-tier, then named middle levels (no codes, may contain spaces), and the
  **office code last**. `fullParentPathName` has the same number of pieces.
- `uiLink` looks like `https://sam.gov/workspace/contract/opp/<noticeId>/view`.
- Volume: 5,127 notices posted in 7 days (all NAICS), so about 730 a day and about 3
  daily-delta pages.

⚠ still to verify: whether `ncode` accepts more than one code, whether an amendment keeps
the same `noticeId`, and when the daily request count resets.

**Set-aside codes** (docs table): SBA, SBP, 8A, 8AN, HZC, HZS, SDVOSBC, SDVOSBS, WOSB, WOSBSS, EDWOSB, EDWOSBSS, LAS, IEE, ISBEE, BICiv, VSA, VSS.

Fixtures: `sam_opportunities/docs_example_1.json`, `docs_example_2.json`, synthetic `amendment_v1/v2.json`.

## 2. SAM.gov Contract Awards API (not used in M1)
| Item | Value |
|---|---|
| Docs | `_apidocs/contract-awards.md` (open.gsa.gov/api/contract-awards) |
| Endpoint | `GET https://api.sam.gov/contract-awards/v1/search` (`deletedStatus=yes` for deletions from the last 6 months) |
| Daily limits | 10/day non-federal without a role; 1,000/day with a role, federal, or non-federal system account; 10,000/day federal system account |
| Paging | `limit` max 100 (default 10); only the first 400,000 records |
| Extract mode | `format=csv` or `json` → async file link, up to 1,000,000 records (replace `REPLACE_WITH_API_KEY` in the link) |
| Useful params | `lastModifiedDate=[MM/DD/YYYY,MM/DD/YYYY]`, `naicsCode`, `piid`, `referencedIdvPiid`, `solicitationID`, `contractingOfficeCode`, `typeOfSetAsideCode`, `ultimateCompletionDate`, `numberOfOffersReceived` |
| Data | DoD awards with a signed date under 90 days old are hidden from public ("revealed") data |

Kept as a fallback for fresher deltas. USAspending covers M1 (see DECISIONS.md).

## 3. USAspending.gov award downloads (no key)
Used by `app ingest awards-backfill` and `awards-delta`.

| Item | Value |
|---|---|
| Docs | `usaspending_api/api_contracts/contracts/v2/download/awards.md`, `download/status.md` |
| Start | `POST https://api.usaspending.gov/api/v2/download/awards/` with `{"filters": {...}, "file_format": "csv", "limit"?: n}` → `file_name`, `file_url`, `status_url` |
| Filters we use | `award_type_codes` (`A`,`B`,`C`,`D`,`IDV_A`,`IDV_B`,`IDV_B_A`,`IDV_B_B`,`IDV_B_C`,`IDV_C`,`IDV_D`,`IDV_E`), `naics_codes: {"require": [...]}`, `time_period: [{start_date, end_date, date_type}]` |
| `date_type` | `action_date` (backfill), `last_modified_date` (delta). Also allowed: `date_signed`, `new_awards_only` (enum in `common/validator/award_filter.py`) |
| Poll | `GET /api/v2/download/status?file_name=...` → `status` ∈ `ready`/`running`/`finished`/`failed`, `total_rows`, `file_url`, `message` |
| Row cap | 500,000 rows per download (`MAX_DOWNLOAD_LIMIT` in `settings.py`). Hitting it means the file is truncated, so the chunk is split. |
| Rate limit | None documented. We space requests ≥ 0.5 s apart and back off on 429/5xx. |
| Not used | `/api/v2/bulk_download/awards/` (no NAICS filter); monthly archive files (whole agencies, all NAICS) |

**Award-level contract columns** (286 in total; full ordered list in
`pipeline/tests/fixtures/usaspending/award_d1_columns.txt`). Columns we load:

`contract_award_unique_key` (primary key), `award_id_piid`, `parent_award_id_piid`,
`award_type_code`, `award_type`, `idv_type_code`, `awarding_agency_code/_name`,
`awarding_sub_agency_code/_name`, `awarding_office_code/_name`, `recipient_uei`,
`recipient_name`, `recipient_parent_uei`, `recipient_parent_name`, `cage_code`,
`recipient_state_code`, `naics_code`, `naics_description`, `product_or_service_code`,
`solicitation_identifier`, `type_of_set_aside_code`, `type_of_set_aside`,
`extent_competed_code`, `number_of_offers_received`, `total_obligated_amount`,
`current_total_value_of_award`, `potential_total_value_of_award`,
`period_of_performance_start_date`, `period_of_performance_current_end_date`,
`period_of_performance_potential_end_date`, `ordering_period_end_date`,
`award_base_action_date`, `award_latest_action_date`,
`prime_award_base_transaction_description`, `last_modified_date`, `usaspending_permalink`.
Business-type flags: `c8a_program_participant`, `sba_certified_8a_joint_venture`,
`historically_underutilized_business_zone_hubzone_firm`,
`service_disabled_veteran_owned_business`, `veteran_owned_business`,
`women_owned_small_business`, `economically_disadvantaged_women_owned_small_business`,
`woman_owned_business`, `small_disadvantaged_business`,
`self_certified_small_disadvantaged_business`,
`contracting_officers_determination_of_business_size`.

✅ Live findings (2026-10-06, 25-row sample, NAICS 541512):
- The header is exactly the documented 286 columns, in order.
- Business-type flags are `t`/`f`. `last_modified_date` looks like `2026-09-28 13:10:18+00`.
  Period-of-performance end dates look like `2030-12-09 00:00:00`; dates such as
  `period_of_performance_start_date` are `YYYY-MM-DD`. All cast correctly.
- The CSV in the zip is named `Contracts_PrimeAwardSummaries_<timestamp>_1.csv`.
- `awarding_agency_code` can be 3 or 4 characters (`005`, `1100`).
- `solicitation_identifier` is often empty on task orders. This matters for M2's
  same-solicitation link.
- For award-level files, `date_type: action_date` matches an award by its **latest** action
  date, so each award appears in exactly one window. Evidence: a 30-day window returned
  contracts with `award_base_action_date` from 2017 to 2025, all with
  `award_latest_action_date` inside the window. In the 3-year backfill, the FY2024 and
  FY2025 files shared no award with each other or with FY2026: the award count grew by
  exactly the rows written. An award modified later moves to a newer window; the daily
  `last_modified_date` delta picks that change up.
- Size (Neon, 2026-10-06), 11 vertical codes. Awards per window: about 124,400 (FY2026),
  96,200 (FY2025) and 85,500 (2023-10-06 to 2024-09-30). With 306,905 awards and 27,689
  entities, `pg_database_size` is 263 MB, about 760 bytes per award with indexes. Neon's
  dashboard showed about 200 MB when the database held the first 124,568 awards.

⚠ still to verify: whether IDVs come in the same file (the sample had only `A`, `C`, `D`).

Fixtures: `usaspending/docs_download_awards_response.json`,
`docs_download_status_finished.json`, `award_d1_columns.txt`, synthetic
`contracts_prime_award_summaries_*.csv`.

## 4. SAM.gov Federal Hierarchy API (not used)
`_apidocs/fh-public-api.md`: `https://api.sam.gov/prod/federalorganizations/v1/orgs`.
API key required; 10 requests/day non-federal, 1,000 federal; max 100 records per page.
Agencies and offices come from notice and award codes instead (DECISIONS.md).

## 5. GSA CALC+ API (Phase 2, not in MVP)
`_apidocs/dx-calc-api.md`. Not read in detail yet.

## 6. FPDS.gov: retired, do not use
Per CLAUDE.md, the public FPDS site shut down on 2026-02-24. The Contract Awards API docs
include an "FPDS vs SAM" PDF (`contract-awards/v1/FPDSvsSAM-ContractDataAPI.pdf`) for the
field mapping.

## 7. SBA Small Business Search: do not scrape
Per CLAUDE.md, use official APIs and bulk files only.
