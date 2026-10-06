# Data sources

> **Status: NOT VERIFIED.** This is a skeleton created in M0. Nothing below has been checked against the official docs yet. The docs hosts were not reachable from the build environment (see DECISIONS.md).
> In M1, fill every section from the official docs **before** writing a client, save one real response per endpoint under `pipeline/tests/fixtures/`, and record the date verified.
> Never invent field names: if the docs are unclear, build from a saved real response.

Each section uses the same template:

| Item | Value |
|---|---|
| Official docs | |
| Auth | |
| Base URL | |
| Endpoints used | |
| Key parameters | |
| Paging | |
| Rate / daily limits | |
| Fields we use (exact names) | |
| Fixture file(s) | |
| Last verified | |

---

## 1. SAM.gov Get Opportunities API (notices)
Planned use: M1 `ingest opportunities` (daily delta + 12-month backfill).

| Item | Value |
|---|---|
| Official docs | https://open.gsa.gov/api/get-opportunities-public-api/ (from CLAUDE.md; verify) |
| Auth | API key `SAM_API_KEY` (per CLAUDE.md; verify parameter/header name) |
| Base URL | TODO |
| Endpoints used | TODO |
| Key parameters | TODO |
| Paging | TODO (CLAUDE.md: "page in large batches") |
| Rate / daily limits | TODO (CLAUDE.md: depends on account role) |
| Fields we use (exact names) | TODO |
| Fixture file(s) | TODO |
| Last verified | never |

## 2. SAM.gov Contract Awards API
Planned use: M1 `ingest awards-delta`, **only if USAspending can't cover daily changes**.

| Item | Value |
|---|---|
| Official docs | https://open.gsa.gov/api/contract-awards/ (from CLAUDE.md; verify) |
| Auth | TODO |
| Base URL | TODO |
| Endpoints used | TODO |
| Key parameters | TODO |
| Paging | TODO |
| Rate / daily limits | TODO (CLAUDE.md: 10 requests/day without a SAM.gov role, 1,000 with one; verify) |
| Fields we use (exact names) | TODO |
| Fixture file(s) | TODO |
| Last verified | never |

## 3. USAspending.gov (bulk downloads + API)
Planned use: M1 `ingest awards-backfill` (5 years, vertical NAICS) and preferred source for `ingest awards-delta`. No key (per CLAUDE.md; verify).

| Item | Value |
|---|---|
| Official docs | TODO |
| Auth | TODO |
| Base URL | TODO |
| Endpoints / bulk files used | TODO |
| Key parameters | TODO |
| Paging / file format | TODO |
| Rate / daily limits | TODO |
| Fields we use (exact names) | TODO |
| Fixture file(s) | TODO |
| Last verified | never |

## 4. GSA CALC+ API (Phase 2, not in MVP)
Planned use: labor-rate benchmarks after the MVP. No key (per CLAUDE.md; verify).

| Item | Value |
|---|---|
| Official docs | https://open.gsa.gov/api/dx-calc-api/ (from CLAUDE.md; verify) |
| Last verified | never |

## 5. FPDS.gov: retired, do not use
Per CLAUDE.md, the public FPDS site shut down on Feb 24, 2026. Do not build on FPDS ATOM feeds.

## 6. SBA Small Business Search: do not scrape
Per CLAUDE.md, do not scrape it (or any site whose terms forbid scraping). Use official APIs and bulk files only.
