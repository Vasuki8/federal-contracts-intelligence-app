# Data sources

**How these were read.** The build container can't reach open.gsa.gov, sam.gov or
usaspending.gov. Both sites publish their API docs from public GitHub repositories, so
the docs were read there:

| Docs | Repository @ commit (date) |
|---|---|
| SAM.gov APIs (open.gsa.gov) | `GSA/open-gsa-redesign` @ `cc568a8` (2026-10-05), `_apidocs/*.md` |
| USAspending API contracts + download column definitions | `fedspendingtransparency/usaspending-api` @ `03b9e25` (2026-10-05) |

⚠ **verify live** marks anything the docs leave unclear or contradict. Run
`uv run app ingest sample-fixtures` as soon as the hosts are reachable. It saves real
responses under `pipeline/tests/fixtures/**/live_*` and reports differences.

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
| Paging | `limit` max 1000 (default 1). `offset` "Indicates the page index. Default offset starts with 0". ⚠ verify live: page index vs record offset (`sample-fixtures` checks this). |
| Daily limit | "Request per day are limited based on the federal or non-federal or general roles." **No numbers on this page.** Other SAM APIs document 10/day (non-federal, no role) and 1,000/day (with a role, or federal). Our budget: `SAM_DAILY_REQUEST_LIMIT` (default 10), counted from `raw_files` since 00:00 UTC (⚠ verify the reset time). |
| Errors | 404 = "No Data found" (treated as an empty page); 400 bad request; 500 server error. Messages listed for bad limit, date format, >1-year range, missing or invalid key. |
| Versions | **"This API only provides the latest active version of the opportunity."** Older versions are only in SAM.gov Data Services extracts. |
| Description text | `description` is a link (`.../noticedesc?noticeid=...`) that needs the API key, so **one request per notice**. Not fetched in M1. |
| Response envelope | `totalRecords`, `limit`, `offset`, `opportunitiesData[]`, `links[]` |

**Record fields** (exact names). ⚠ The field table and the example response disagree; the parser accepts both:

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

⚠ verify live: response-deadline format (time zone), whether `ncode` accepts more than one
code, and whether an amendment keeps the same `noticeId`.

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

⚠ verify live: boolean encoding (`t`/`f`?), date-time format, CSV file names in the zip
(the loader finds award files by header, not name), and whether IDVs and contracts share
one file.

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
