"""Core ingest tables: runs, raw archive, reference data, notices and awards (M1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Uppercase, spaces and dashes removed. Keep in sync with pipeline.normalize.normalize_contract_id.
_NORM = "nullif(upper(regexp_replace(coalesce({col}, ''), '[[:space:]-]+', '', 'g')), '')"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ingest_runs (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source text NOT NULL,
            job text NOT NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
            params jsonb NOT NULL DEFAULT '{}'::jsonb,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            rows_in integer NOT NULL DEFAULT 0,
            rows_upserted integer NOT NULL DEFAULT 0,
            requests_made integer NOT NULL DEFAULT 0,
            error text
        );
        CREATE INDEX ingest_runs_source_job_started_idx
            ON ingest_runs (source, job, started_at DESC);

        CREATE TABLE raw_files (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id bigint REFERENCES ingest_runs (id) ON DELETE SET NULL,
            source text NOT NULL,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            path text NOT NULL UNIQUE,
            sha256 char(64) NOT NULL,
            bytes bigint NOT NULL,
            http_status integer,
            request_params jsonb NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX raw_files_source_fetched_idx ON raw_files (source, fetched_at);
        CREATE INDEX raw_files_sha256_idx ON raw_files (sha256);

        CREATE TABLE ingest_chunks (
            source text NOT NULL,
            chunk_key text NOT NULL,
            status text NOT NULL CHECK (status IN ('in_progress', 'done')),
            state jsonb NOT NULL DEFAULT '{}'::jsonb,
            rows_in integer NOT NULL DEFAULT 0,
            rows_upserted integer NOT NULL DEFAULT 0,
            run_id bigint REFERENCES ingest_runs (id) ON DELETE SET NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, chunk_key)
        );

        CREATE TABLE agencies (
            id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            level text NOT NULL CHECK (level IN ('department', 'subtier')),
            code text NOT NULL,
            name text,
            parent_code text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (level, code)
        );

        CREATE TABLE offices (
            id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subtier_code text NOT NULL,
            office_code text NOT NULL,
            name text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (subtier_code, office_code)
        );

        CREATE TABLE naics (
            code text PRIMARY KEY CHECK (code ~ '^[0-9]{2,6}$'),
            title text,
            in_vertical boolean NOT NULL DEFAULT false,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute(f"""
        CREATE TABLE notices (
            notice_id text PRIMARY KEY,
            solicitation_number text,
            solicitation_number_norm text
                GENERATED ALWAYS AS ({_NORM.format(col="solicitation_number")}) STORED,
            title text,
            type text,
            base_type text,
            posted_at timestamptz,
            response_deadline timestamptz,
            naics text,
            psc text,
            set_aside_code text,
            set_aside text,
            department_code text,
            subtier_code text,
            office_code text,
            office_id integer REFERENCES offices (id),
            full_parent_path_code text,
            full_parent_path_name text,
            place_of_performance jsonb,
            pop_state text,
            description_url text,
            attachment_links jsonb NOT NULL DEFAULT '[]'::jsonb,
            contacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            award jsonb,
            ui_link text,
            active boolean,
            archive_type text,
            archive_date date,
            latest_version integer NOT NULL DEFAULT 1,
            content_hash text NOT NULL,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            raw_file_id bigint REFERENCES raw_files (id) ON DELETE SET NULL
        );
        CREATE INDEX notices_solicitation_number_norm_idx ON notices (solicitation_number_norm);
        CREATE INDEX notices_naics_idx ON notices (naics);
        CREATE INDEX notices_response_deadline_idx ON notices (response_deadline);
        CREATE INDEX notices_posted_at_idx ON notices (posted_at);
        CREATE INDEX notices_office_idx ON notices (subtier_code, office_code);

        CREATE TABLE notice_versions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            notice_id text NOT NULL REFERENCES notices (notice_id) ON DELETE CASCADE,
            version integer NOT NULL,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            diff jsonb,
            snapshot jsonb NOT NULL,
            raw_file_id bigint REFERENCES raw_files (id) ON DELETE SET NULL,
            UNIQUE (notice_id, version)
        );

        CREATE TABLE entities (
            uei text PRIMARY KEY,
            name text,
            parent_uei text,
            parent_name text,
            cage text,
            state text,
            business_types jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            last_modified timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE awards (
            award_key text PRIMARY KEY,
            piid text,
            piid_norm text GENERATED ALWAYS AS ({_NORM.format(col="piid")}) STORED,
            referenced_idv_piid text,
            award_type_code text,
            award_type text,
            idv_type_code text,
            awarding_agency_code text,
            awarding_agency_name text,
            awarding_sub_agency_code text,
            awarding_sub_agency_name text,
            awarding_office_code text,
            awarding_office_name text,
            office_id integer REFERENCES offices (id),
            recipient_uei text,
            recipient_name text,
            naics text,
            psc text,
            solicitation_id text,
            solicitation_id_norm text
                GENERATED ALWAYS AS ({_NORM.format(col="solicitation_id")}) STORED,
            set_aside_code text,
            set_aside text,
            extent_competed_code text,
            number_of_offers integer,
            obligated_total numeric,
            current_total_value numeric,
            total_value numeric,
            pop_start date,
            current_end date,
            ultimate_end date,
            ordering_period_end date,
            base_action_date date,
            latest_action_date date,
            description text,
            last_modified timestamptz,
            usaspending_url text,
            raw_file_id bigint REFERENCES raw_files (id) ON DELETE SET NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX awards_piid_norm_idx ON awards (piid_norm);
        CREATE INDEX awards_solicitation_id_norm_idx ON awards (solicitation_id_norm);
        CREATE INDEX awards_office_naics_idx
            ON awards (awarding_sub_agency_code, awarding_office_code, naics);
        CREATE INDEX awards_ultimate_end_idx ON awards (ultimate_end);
        CREATE INDEX awards_recipient_uei_idx ON awards (recipient_uei);
        CREATE INDEX awards_naics_idx ON awards (naics);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE awards;
        DROP TABLE entities;
        DROP TABLE notice_versions;
        DROP TABLE notices;
        DROP TABLE naics;
        DROP TABLE offices;
        DROP TABLE agencies;
        DROP TABLE ingest_chunks;
        DROP TABLE raw_files;
        DROP TABLE ingest_runs;
    """)
