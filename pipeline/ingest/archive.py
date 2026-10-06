"""Raw archive: keep every response or bulk file exactly as received, with a sha256."""

import gzip
import hashlib
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.ingest.http import redact_params

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ArchivedFile:
    id: int
    path: str
    sha256: str
    bytes: int


def _relative_path(source: str, fetched_at: datetime, digest: str, suffix: str) -> Path:
    name = f"{fetched_at:%H%M%S%f}_{digest[:12]}{suffix}"
    return Path(source) / f"{fetched_at:%Y}" / f"{fetched_at:%m}" / f"{fetched_at:%d}" / name


def _insert_row(
    conn: psycopg.Connection[TupleRow],
    *,
    run_id: int | None,
    source: str,
    fetched_at: datetime,
    path: Path,
    digest: str,
    size: int,
    http_status: int | None,
    request_params: Mapping[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO raw_files
            (run_id, source, fetched_at, path, sha256, bytes, http_status, request_params)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            run_id,
            source,
            fetched_at,
            path.as_posix(),
            digest,
            size,
            http_status,
            Jsonb(redact_params(request_params)),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def archive_bytes(
    conn: psycopg.Connection[TupleRow],
    *,
    root: Path,
    source: str,
    content: bytes,
    suffix: str,
    run_id: int | None,
    http_status: int | None,
    request_params: Mapping[str, Any],
    compress: bool = False,
    fetched_at: datetime | None = None,
) -> ArchivedFile:
    """Store `content` and record it. The sha256 is of the bytes as received."""
    when = fetched_at or datetime.now(UTC)
    digest = hashlib.sha256(content).hexdigest()
    relative = _relative_path(source, when, digest, suffix + (".gz" if compress else ""))
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(content, mtime=0) if compress else content
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    file_id = _insert_row(
        conn,
        run_id=run_id,
        source=source,
        fetched_at=when,
        path=relative,
        digest=digest,
        size=len(content),
        http_status=http_status,
        request_params=request_params,
    )
    return ArchivedFile(id=file_id, path=relative.as_posix(), sha256=digest, bytes=len(content))


def archive_file(
    conn: psycopg.Connection[TupleRow],
    *,
    root: Path,
    source: str,
    file: Path,
    suffix: str,
    run_id: int | None,
    http_status: int | None,
    request_params: Mapping[str, Any],
    fetched_at: datetime | None = None,
) -> ArchivedFile:
    """Move a downloaded file (e.g. a bulk zip) into the archive and record it."""
    when = fetched_at or datetime.now(UTC)
    hasher = hashlib.sha256()
    size = 0
    with file.open("rb") as handle:
        while block := handle.read(_CHUNK):
            hasher.update(block)
            size += len(block)
    digest = hasher.hexdigest()
    relative = _relative_path(source, when, digest, suffix)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(file, target)
    file_id = _insert_row(
        conn,
        run_id=run_id,
        source=source,
        fetched_at=when,
        path=relative,
        digest=digest,
        size=size,
        http_status=http_status,
        request_params=request_params,
    )
    return ArchivedFile(id=file_id, path=relative.as_posix(), sha256=digest, bytes=size)


def read_archived(root: Path, path: str) -> bytes:
    """Read an archived file back, undoing gzip if it was compressed on write."""
    data = (root / path).read_bytes()
    return gzip.decompress(data) if path.endswith(".gz") else data
