# Federal contracts intelligence app

Helps certified small businesses in IT and professional services decide which federal contracts to bid on, and how to win them. See `CLAUDE.md` for the product and rules, and `BUILD_PLAN.md` for the milestones.

## Layout

```
pipeline/     Python package: ingest, match, recompetes, briefs, CLI (`uv run app`)
migrations/   Alembic (owns every schema change)
web/          Next.js app (public pages, logged-in app, admin)
docs/         data-sources.md, DECISIONS.md
data/raw/     local raw archive (gitignored)
```

## Requirements

Docker, [uv](https://docs.astral.sh/uv/), Node 22 and pnpm 10.

## Quick start

```bash
cp .env.example .env              # then fill in keys as milestones need them
docker compose up -d db           # Postgres 16 (also creates the fci_test database)
uv sync                           # Python deps
uv run alembic upgrade head       # migrate
uv run app doctor                 # check settings, DB connection and migrations
cd web && pnpm install && pnpm dev   # http://localhost:3000, health at /api/health
```

## Checks

```bash
./scripts/check.sh                # everything CI runs: lint, types, migrations, tests, build
```

Or individually: `uv run ruff check`, `uv run mypy`, `uv run pytest`, and in `web/`: `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`.

After adding a migration, regenerate the web's DB types with `cd web && pnpm db:codegen` and commit `web/src/db/types.ts`. CI fails if they drift.

## Hosted database and scheduled ingest (Neon + GitHub Actions)

1. In the [Neon console](https://console.neon.tech), create a project (Postgres **16 or newer**; a US East region is closest to the data sources and GitHub's runners).
2. On the project dashboard, click **Connect**, turn **Connection pooling off**, and copy the connection string. The host must not contain `-pooler`: ingest needs session locks and temp tables.
3. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**, name `DATABASE_URL`, paste the string. `SAM_API_KEY` goes there too. Optionally add the *variable* `SAM_DAILY_REQUEST_LIMIT`.
4. **Actions → Ingest → Run workflow**, choose `awards-backfill` (USAspending, a few hours, no key), then `notice-archive` (the last two fiscal years of archived notices).
5. The schedule then runs `daily` every morning:
   1. every active notice from SAM.gov's daily CSV (with descriptions);
   2. on Mondays, the archived notices;
   3. the API notices delta and backfill (attachments, within the SAM budget);
   4. the awards delta;
   5. the incumbent matcher and the recompete list.

Check progress in each run's **Status** step (`app status`).

### Labeling matches (accuracy check for M2)

1. **Actions → Ingest → Run workflow**, choose `label-sheet`. When it finishes, open the run and download the `matching-labels-…` artifact (a zip containing `matching-labels.csv`).
2. Open the CSV in Excel or Google Sheets. For each notice, compare it (SAM.gov link) with candidates 1–5 (contract number, company, value, end date, USAspending link, reasons). In the `answer` column write:
   - the candidate's number, if it is the contract this notice replaces;
   - `other`, if the incumbent isn't listed (put its contract number in `notes` if you know it);
   - `none`, if it's new work with no incumbent;
   - `unsure`, if you can't tell.
3. Save it as CSV and upload it to the repository on GitHub (**Add file → Upload files**), or send it over. It is imported with `uv run app match import-labels <file>` into `pipeline/tests/eval/labeled_matches.csv`.
4. **Actions → Ingest → Run workflow → `match-eval`** reports precision against the labels. The target is ≥ 90% precision on shown incumbents. The repository is public, so run logs and the raw-archive artifacts are public too (public government data; secrets are masked).
