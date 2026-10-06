# BUILD_PLAN.md — MVP build plan

Read `CLAUDE.md` first. Build one milestone at a time, in order. Each milestone ends with its acceptance checks passing.

**MVP goal:** a certified small business in IT or professional services signs up, sets its profile, and every morning receives matched federal opportunities, each showing a plain-English brief and the incumbent. They can save notices, get amendment alerts, see upcoming recompetes, and log bid outcomes. Public SEO pages bring in traffic. Billing has two tiers: Free and Solo ($29/mo).

**Not in the MVP:** bid/no-bid scoring, price benchmarks, vehicle map, teaming, team accounts, the win-probability model, proposal writing, state/local bids.

---

## M0 — Project setup

- Monorepo layout as in `CLAUDE.md`; Docker Compose with Postgres 16.
- `pipeline` package with a CLI entry point `app` (Typer); ruff, mypy and pytest configured.
- `web` Next.js app with Tailwind, ESLint and Vitest; kysely + kysely-codegen wired to the DB.
- Alembic set up with an empty first migration.
- `.env.example` listing: `DATABASE_URL`, `SAM_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `RESEND_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `AUTH_SECRET`, `RAW_ARCHIVE_DIR`.
- GitHub Actions CI: lint + type-check + tests for both packages.
- Create `docs/data-sources.md`, `docs/DECISIONS.md`.

**Done when:** `docker compose up`, migrations, `uv run pytest` and `pnpm test` all pass from a clean clone, and CI is green.

---

## M1 — Data ingestion

First read the official docs for each source and fill in `docs/data-sources.md` (endpoints, parameters, limits, fields, sample response saved as a fixture).

**Core tables** (refine as needed; record changes in DECISIONS.md):
- `ingest_runs` (source, started_at, finished_at, status, rows_in, rows_upserted, error)
- `raw_files` (source, fetched_at, path, sha256, request_params)
- `agencies`, `offices` (agency/sub-tier/office codes and names)
- `naics` (code, title, in_vertical boolean)
- `notices` (notice_id, solicitation_number, title, type, posted_at, response_deadline, naics, psc, set_aside_code, office_id, place_of_performance, description_url, attachment_links jsonb, contacts jsonb, active, latest_version)
- `notice_versions` (notice_id, version, fetched_at, diff jsonb), one row per amendment or change
- `entities` (uei, name, parent_uei, business_types jsonb, state, cage)
- `awards` (piid, referenced_idv_piid, mod_number, awardee_uei, agency/office, naics, psc, solicitation_id, set_aside_type, extent_competed, number_of_offers, obligated_total, total_value, pop_start, current_end, ultimate_end, award_type, last_modified)

**Jobs:**
1. `ingest opportunities`: SAM.gov Get Opportunities API. Daily delta plus a backfill (default 12 months). Store every raw page. Upsert notices. Detect changes to an existing notice and write a `notice_versions` row.
2. `ingest awards-backfill`: USAspending bulk download (no key), filtered to vertical NAICS codes, default 5 years. Load contract awards and IDVs, collapsing modifications to one current row per award (keep the transaction history table if useful).
3. `ingest awards-delta`: daily changes. Prefer USAspending. Use the SAM.gov Contract Awards API only if needed, and stay within its daily limit.
4. Every job: idempotent, resumable, rate-limited with exponential backoff, logs to `ingest_runs`, archives raw files with a sha256.

**Done when:**
- Backfill loads ≥ 12 months of notices and ≥ 5 years of vertical awards locally.
- Re-running any job produces no duplicate rows.
- Parser tests run against saved fixtures, including a notice amendment case.
- `uv run app status` prints the last run, row counts and errors per source.

---

## M2 — Incumbent matching and recompetes (the core moat)

**Matcher** (`app match notices`): for each active notice in the vertical, find the award it replaces.

1. **Same-solicitation link (history, not incumbents):** `awards.solicitation_id` = `notices.solicitation_number` (normalize: uppercase, strip spaces and dashes). This links a notice to the award *made from it*. Use it to label past notices with their winner and to build evaluation data. It does not find the incumbent of a new recompete, which usually has a new solicitation number.
2. **Explicit reference:** search the notice title, description and attachment text for contract-number patterns (PIIDs) that exist in `awards`. A hit is the strongest incumbent signal (score near 1.0).
3. **Candidates:** awards from the same office (or sub-tier agency), the same or a related NAICS/PSC, with `ultimate_end` within −6 to +18 months of the notice's response deadline, and not already superseded.
4. **Score** each candidate from 0 to 1 using weighted features: explicit reference, office match, NAICS match, PSC match, title/description similarity (TF-IDF or trigram), end-date proximity, set-aside match, and dollar size consistent with any value in the notice. Keep the weights in config.
5. Store every candidate in `notice_award_matches` (notice_id, piid, score, method, evidence jsonb, status: auto | confirmed | rejected).
6. Show a confirmed incumbent only when the top score ≥ `INCUMBENT_THRESHOLD` (start at 0.8) and beats the runner-up by a clear margin. Otherwise show up to 3 "possible incumbents" with reasons.

**Evaluation:** create `pipeline/tests/eval/labeled_matches.csv` with 100 hand-labeled notices (I'll help label them). The script `app match eval` reports precision and recall at the threshold, and the result goes into `docs/matching-eval.md`. **Target: ≥ 90% precision on shown incumbents.**

**Recompetes** (`app recompetes refresh`): awards in the vertical whose `ultimate_end` falls 6–24 months from today, with incumbent, agency, office, total value, set-aside, number of offers and any linked notice. Materialize into a `recompetes` table for fast queries.

**Admin review queue:** a page under `/admin/matches` listing low-confidence matches with their evidence, plus confirm/reject buttons. Decisions are stored and fed back into the evaluation set.

**Done when:** the eval script meets the precision target; recompetes refresh in under 5 minutes; matcher unit tests cover normalization, scoring and tie-breaking.

---

## M3 — Logged-in app core

- **Auth:** email magic link (Auth.js). Each user belongs to an `org` (one user per org in the MVP).
- **Onboarding** (under 2 minutes): NAICS codes (pre-checked from the vertical), certifications (8(a), SDVOSB, WOSB, EDWOSB, HUBZone, small business), target agencies (optional), states (optional), and optional UEI to pull the firm's past awards.
- **Feed:** matched active notices sorted by fit, then deadline. Fit score v0 is rules-based: NAICS match, set-aside eligibility given certifications, agency preference, place of performance, and time left to respond. Show why each notice matched.
- **Notice page:**
  - **Brief**: five lines covering what is being bought, for whom, where, the set-aside and deadline, and the key requirements. Generate it with the Anthropic API from the notice text, cache it per notice version, and regenerate on amendment. Use only facts in the source text; if a field is missing, say "Not stated". Track token cost per run.
  - **Incumbent card**: the confirmed incumbent (value, end date, number of offers, link to award) or possible incumbents with reasons.
  - Raw details: dates, contacts, attachments and a link to SAM.gov.
- **Saved notices** with a deadline countdown.
- **Outcome logging** on every notice card and page, one click: Bidding / Not bidding (optional reason: not a fit, incumbent too strong, too large, no time, other) / Submitted / Won / Lost. Store outcomes as events (`outcome_events`) and never overwrite them.
- **My results** page: counts and win rate from the user's own outcomes.
- **Recompete calendar** page filtered to the user's profile.

**Done when:** Playwright tests pass for sign-up → onboarding → feed → notice → save → log outcome; pages load in under 1.5 s on local data.

---

## M4 — Alerts

- **Daily digest email** (Resend), sent at 6:30 local time: new matched notices (up to 10, each with its 1-line brief and incumbent), deadlines within 7 days on saved notices, and new recompetes in the profile. Include one-click outcome links in the email.
- **Amendment alert:** when a saved notice gets a new version, email what changed (title, deadline, set-aside, new attachments).
- An unsubscribe link and email preferences page.
- `alerts_sent` table to prevent duplicate sends.

**Done when:** a dry-run command renders tomorrow's digests to HTML files for review, and the duplicate-send test passes.

---

## M5 — Public SEO pages (+ AdSense slots)

Google penalizes thin auto-generated pages, so **each page must carry computed insight and is only published when there is enough data.**

- `/agencies/[agency]/[naics]`: award volume by year, top vendors, typical number of offers, set-aside mix, upcoming recompetes, open notices.
- `/companies/[uei]`: awards in the vertical, agencies served, contracts ending soon.
- `/recompetes/[naics]`: upcoming recompetes list with a sign-up call to action.
- `/notices/[id]`: a public preview showing the brief and incumbent card but no outcome tools, with sign-up to save.
- **Publish rule:** index a page only if it has ≥ 10 awards (or ≥ 3 open notices); otherwise mark it `noindex`. Add a sitemap, canonical URLs, structured data (Organization/Dataset where appropriate) and incremental static regeneration.
- **AdSense:** one reserved, clearly separated ad slot per public page, disabled by an env flag until approved. Never show ads in the logged-in app.

**Done when:** Lighthouse SEO ≥ 95 on sample pages; the sitemap only lists pages that pass the publish rule.

---

## M6 — Billing

- Stripe Checkout and Customer Portal; webhooks update `subscriptions`.
- **Free:** public pages, 1 saved search, weekly digest, 5 saved notices.
- **Solo ($29/mo or $290/yr):** daily digest, unlimited saved notices, amendment alerts, full incumbent cards, recompete calendar, CSV export.
- Gate features server-side, not just in the UI.
- 14-day Solo trial with no card required.

**Done when:** webhook tests pass for create, upgrade, cancel and failed payment; downgrade removes access cleanly.

---

## M7 — Hardening and launch

- Monitoring: alert (email to me) if any ingest job fails or has not run in 26 hours; a `/admin/health` page.
- Nightly DB backups; raw archive pushed to object storage.
- Error tracking (Sentry or similar; ask first).
- Privacy policy and terms pages (placeholders for me to fill in), plus a cookie notice for AdSense.
- Load test the feed and public pages.
- Write a `docs/RUNBOOK.md`: how to re-run ingests, rotate keys and restore backups.

**Done when:** a fresh production deploy runs a full daily cycle (ingest → match → recompetes → briefs → digest) without manual steps.

---

## After the MVP (do not build yet)

Phase 2: bid/no-bid score with reasons, price benchmarks (CALC+), vehicle map (task orders → parent IDVs → holders), teaming matches, team accounts and shared pipeline, Pro and Team tiers.
Phase 3: win-probability model trained on `outcome_events`, buying-office profiles, forecast tracking, AI RFP compliance checklist, API for consultants.
