"""Incumbent matching and recompetes (M2).

- notice_descriptions: description text per notice version (1 SAM request each).
- notice_award_matches: every candidate link between a notice and an award, with its
  score, evidence and what the app shows; human decisions live in `status`.
- match_reviews: append-only admin decisions, fed back into the evaluation.
- recompetes: vertical awards ending in 6-24 months, refreshed by `app recompetes refresh`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE notice_descriptions (
            notice_id text NOT NULL REFERENCES notices (notice_id) ON DELETE CASCADE,
            version integer NOT NULL,
            text text,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            raw_file_id bigint REFERENCES raw_files (id) ON DELETE SET NULL,
            PRIMARY KEY (notice_id, version)
        );

        CREATE TABLE notice_award_matches (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            notice_id text NOT NULL REFERENCES notices (notice_id) ON DELETE CASCADE,
            award_key text NOT NULL REFERENCES awards (award_key) ON DELETE CASCADE,
            piid text,
            kind text NOT NULL CHECK (kind IN ('incumbent', 'resulting_award')),
            method text NOT NULL CHECK (method IN (
                'explicit_reference', 'candidate', 'same_solicitation', 'award_notice_number'
            )),
            score numeric(5, 4) NOT NULL CHECK (score BETWEEN 0 AND 1),
            rank integer,
            shown text NOT NULL DEFAULT 'hidden'
                CHECK (shown IN ('incumbent', 'possible', 'hidden')),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'auto'
                CHECK (status IN ('auto', 'confirmed', 'rejected')),
            matcher_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (notice_id, award_key, kind)
        );
        CREATE INDEX notice_award_matches_award_idx ON notice_award_matches (award_key);
        CREATE INDEX notice_award_matches_shown_idx
            ON notice_award_matches (notice_id, kind, shown);

        CREATE TABLE match_reviews (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            match_id bigint REFERENCES notice_award_matches (id) ON DELETE SET NULL,
            notice_id text NOT NULL,
            award_key text NOT NULL,
            decision text NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
            reviewer text,
            note text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX match_reviews_notice_idx ON match_reviews (notice_id, award_key);

        CREATE TABLE recompetes (
            award_key text PRIMARY KEY REFERENCES awards (award_key) ON DELETE CASCADE,
            piid text,
            award_type_code text,
            recipient_uei text,
            recipient_name text,
            awarding_agency_code text,
            awarding_agency_name text,
            awarding_sub_agency_code text,
            awarding_sub_agency_name text,
            awarding_office_code text,
            awarding_office_name text,
            naics text,
            psc text,
            set_aside_code text,
            set_aside text,
            number_of_offers integer,
            total_value numeric,
            obligated_total numeric,
            ultimate_end date NOT NULL,
            linked_notice_id text REFERENCES notices (notice_id) ON DELETE SET NULL,
            refreshed_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX recompetes_ultimate_end_idx ON recompetes (ultimate_end);
        CREATE INDEX recompetes_naics_idx ON recompetes (naics);
        CREATE INDEX recompetes_sub_agency_idx ON recompetes (awarding_sub_agency_code);

        CREATE INDEX awards_sub_agency_end_idx
            ON awards (awarding_sub_agency_code, ultimate_end);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX awards_sub_agency_end_idx;
        DROP TABLE recompetes;
        DROP TABLE match_reviews;
        DROP TABLE notice_award_matches;
        DROP TABLE notice_descriptions;
    """)
