# CLAUDE.md — Federal contracts intelligence app

## What we are building

A web app that helps **certified small businesses in IT and professional services** decide which federal contracts to bid on and how to win them. Other tools already do opportunity search, so search is not the product. The product answers the question that comes after search: **"Should we bid on this, and how do we win it?"**

Every opportunity in the app shows:
- a five-line plain-English brief
- the **incumbent**: who holds the current contract, its value, its end date and how many bids it drew
- the set-aside, deadline and fit against the user's profile

Target user: the owner or business-development lead of a 1–50 person firm holding 8(a), SDVOSB, WOSB, EDWOSB or HUBZone status, or plain small-business status. They have about 20 minutes a day. Write every UI string in plain English and keep jargon to a minimum.

Business model: free public SEO pages (with Google AdSense) feed sign-ups. Paid tiers are Solo $29/mo, Pro $79/mo and Team $199/mo. **The MVP ships Free + Solo only.** Ads appear on public pages only, never inside the logged-in app.

The full plan is in `BUILD_PLAN.md`. Work one milestone at a time.

## Moat principles (shape every design decision)

1. **Linked data is the moat.** Each notice is linked to the contract it replaces (the incumbent). Store match evidence and a confidence score for every link.
2. **User outcomes are the deepest moat.** Make logging "Bidding / Not bidding / Won / Lost" a single click, and keep every outcome event. A win-probability model will be trained on these later.
3. **Never show a guess as a fact.** Show an incumbent as confirmed only above the confidence threshold. Below it, show "Possible incumbents" with the reason for each.
4. **Archive everything raw.** Government data gets changed or removed. Keep every raw API response or bulk file we ingest, with its fetch timestamp.

## Tech stack (default; ask before changing)

- **Database:** PostgreSQL 16 (Docker locally; Neon or Supabase in production)
- **Data pipeline:** Python 3.12, `uv`, `httpx`, `polars`, `psycopg`, `pydantic`. Alembic owns **all** schema migrations.
- **Web app:** Next.js (App Router) + TypeScript + Tailwind, `kysely` with types generated from the DB (`kysely-codegen`)
- **Auth:** Auth.js (email magic link). **Billing:** Stripe. **Email:** Resend.
- **LLM briefs:** Anthropic API. Read the model name from the `ANTHROPIC_MODEL` env var; never hard-code it.
- **Jobs:** cron-triggered Python CLI commands (GitHub Actions schedule in production)
- **Tests:** pytest (pipeline), Vitest (web), Playwright (critical flows)

## Repo layout

```
/pipeline        Python package: ingest, normalize, match, recompete, briefs, CLI
/pipeline/tests  pytest, with recorded API fixtures in tests/fixtures
/web             Next.js app (public pages + logged-in app + admin)
/migrations      Alembic
/data/raw        local raw archive (gitignored); S3-compatible bucket in prod
/docs            data-sources.md, DECISIONS.md, matching-eval.md
```

## Data source rules (critical)

- **Read the official docs before writing any client**, and record endpoints, parameters, limits and field names in `docs/data-sources.md`. Never invent field names. If the docs are unclear, fetch one real response, save it as a fixture and build from that.
- **SAM.gov Get Opportunities API** (open.gsa.gov/api/get-opportunities-public-api): needs an API key. Daily request limits depend on account role. Page in large batches and stay under the limit.
- **SAM.gov Contract Awards API** (open.gsa.gov/api/contract-awards): only **10 requests/day without a SAM.gov role** and 1,000 with one. Use it only for small daily deltas. Use **USAspending.gov bulk downloads** (no key) for history backfill.
- **FPDS.gov is retired** (public site shut down Feb 24, 2026). Do not build on FPDS ATOM feeds.
- **GSA CALC+ API** (open.gsa.gov/api/dx-calc-api): no key; labor-rate benchmarks (Phase 2).
- **Do not scrape** SBA Small Business Search or any site whose terms forbid it. Prefer official APIs and bulk files.
- Every ingest job must be **idempotent** (safe to re-run), **resumable**, rate-limited with backoff, and must log a row in `ingest_runs`.
- Missing API key → stop and ask the user to add it to `.env`. Never commit secrets.

## Starting vertical (configurable in `pipeline/config/verticals.yaml`)

IT and professional services NAICS codes: 541511, 541512, 541513, 541519, 518210, 541611, 541612, 541618, 541690, 541330, 541990.

## Working agreement for Claude Code

- Start every milestone in **plan mode**: list files, schema changes and tests, then wait for approval.
- Before adding a dependency, explain why it is needed and ask.
- Write tests alongside code. Parsers and the matcher need fixture-based tests.
- Record non-obvious choices in `docs/DECISIONS.md` (date, decision, why).
- Keep functions small and typed. No `any` in TypeScript; Python passes `mypy --strict` on `/pipeline`.
- After each milestone: run all tests and linters, summarize what was built, list anything not done, and suggest the next step. Don't start the next milestone until asked.

## Commands (keep this section updated)

```
cp .env.example .env                 # first time only
docker compose up -d db              # local Postgres 16 (+ fci_test DB for pytest)
uv sync                              # Python deps
uv run alembic upgrade head          # migrate
uv run alembic revision --rev-id 0003 -m "..."   # next migration (sequential ids)
uv run app doctor                    # settings, DB connection, migration status
uv run app status                    # last run + errors per job, backfill progress, row counts
uv run ruff check && uv run ruff format --check && uv run mypy
uv run pytest
cd web && pnpm dev | pnpm test | pnpm lint | pnpm typecheck | pnpm build
cd web && pnpm db:codegen            # regenerate src/db/types.ts after a migration
./scripts/check.sh                   # run every CI check locally

uv run app ingest sample-fixtures    # first live run: save real fixtures, compare with docs
# (or GitHub → Actions → "Live sample" → Run workflow: same command, uses the SAM_API_KEY secret)
uv run app ingest awards-backfill --years 5          # USAspending, resumable
uv run app ingest awards-delta                       # daily
uv run app ingest opportunities                      # daily SAM delta (run before backfill)
uv run app ingest opportunities --backfill           # resume / start 12 months; no-op once complete
# Hosted: GitHub → Actions → "Ingest" (daily schedule + manual awards-backfill); DB = Neon (README)

# Planned (not built yet):
uv run app match notices                             # M2
uv run app recompetes refresh                        # M2
```
