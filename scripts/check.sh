#!/usr/bin/env bash
# Run every check CI runs, locally. Needs the database: docker compose up -d db
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$*"; }

step "Python: install"
uv sync --locked
step "Python: lint";        uv run ruff check
step "Python: format";      uv run ruff format --check
step "Python: type-check";  uv run mypy
step "Migrate";             uv run alembic upgrade head
step "CLI: doctor";         uv run app doctor
step "Python: tests";       uv run pytest

cd web
step "Web: install";        pnpm install --frozen-lockfile
step "Web: lint";           pnpm lint
step "Web: type-check";     pnpm typecheck
step "Web: tests";          pnpm test
step "Web: build";          pnpm build
step "Web: DB types match migrations"
pnpm db:verify

printf '\nAll checks passed.\n'
