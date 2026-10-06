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
