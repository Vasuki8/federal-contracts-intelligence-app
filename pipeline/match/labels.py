"""The labeling spreadsheet: export notices with their candidate contracts for a person to
judge, and import the answers into `pipeline/tests/eval/labeled_matches.csv`.

To avoid anchoring the labeler, the sheet does not say which candidate the matcher picked,
and candidates are listed in a shuffled (but repeatable) order. The sample mixes notices
where an incumbent would be shown (to measure precision) with notices that only have
possible incumbents (to measure what the matcher misses)."""

import csv
import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.match.candidates import Conn

CANDIDATES_PER_NOTICE = 5
SHOWN_SHARE = 0.6
LABELS_FILE = Path(__file__).resolve().parent.parent / "tests" / "eval" / "labeled_matches.csv"
LABEL_COLUMNS = ("notice_id", "award_key", "answer", "source", "labeled_at", "notes")
ANSWERS = {"none": "none", "other": "other", "unsure": "unsure", "": "unsure"}
CANDIDATE_FIELDS = (
    "contract",
    "company",
    "value",
    "ends",
    "office",
    "description",
    "why",
    "link",
    "id",
)


def sheet_columns() -> list[str]:
    columns = ["notice_id", "sam_link", "title", "agency", "deadline", "solicitation_number"]
    for n in range(1, CANDIDATES_PER_NOTICE + 1):
        columns += [f"c{n}_{name}" for name in CANDIDATE_FIELDS]
    return [*columns, "answer", "notes"]


def _sample(conn: Conn, shown: str, limit: int, seed: str) -> list[str]:
    """Notices whose best automatic match is `shown`, in a repeatable random order."""
    rows = conn.execute(
        """
        SELECT n.notice_id FROM notices n
        WHERE n.active AND EXISTS (
            SELECT 1 FROM notice_award_matches m
            WHERE m.notice_id = n.notice_id AND m.kind = 'incumbent' AND m.shown = %s)
        ORDER BY md5(n.notice_id || %s)
        LIMIT %s
        """,
        (shown, seed, limit),
    ).fetchall()
    return [row[0] for row in rows]


def label_sheet(conn: Conn, size: int, seed: str = "m2") -> list[dict[str, str]]:
    """Rows for the labeling spreadsheet (see the module docstring)."""
    with_incumbent = _sample(conn, "incumbent", round(size * SHOWN_SHARE), seed)
    possible = _sample(conn, "possible", size - len(with_incumbent), seed)
    return [_sheet_row(conn, notice_id, seed) for notice_id in with_incumbent + possible]


def _sheet_row(conn: Conn, notice_id: str, seed: str) -> dict[str, str]:
    notice = conn.execute(
        """
        SELECT notice_id, coalesce(ui_link, 'https://sam.gov/opp/' || notice_id || '/view'),
               title, full_parent_path_name, response_deadline::date, solicitation_number
        FROM notices WHERE notice_id = %s
        """,
        (notice_id,),
    ).fetchone()
    assert notice is not None
    row = dict(zip(sheet_columns()[:6], (_text(value) for value in notice), strict=True))
    candidates = conn.execute(
        """
        SELECT a.piid, a.recipient_name, coalesce(a.total_value, a.current_total_value),
               coalesce(a.ultimate_end, a.ordering_period_end), a.awarding_office_name,
               a.description, m.evidence -> 'reasons', a.usaspending_url, a.award_key
        FROM notice_award_matches m JOIN awards a ON a.award_key = m.award_key
        WHERE m.notice_id = %s AND m.kind = 'incumbent' AND m.status <> 'rejected'
        ORDER BY m.rank NULLS LAST, m.award_key
        LIMIT %s
        """,
        (notice_id, CANDIDATES_PER_NOTICE),
    ).fetchall()
    shuffled = list(candidates)
    random.Random(hashlib.sha256(f"{seed}:{notice_id}".encode()).hexdigest()).shuffle(shuffled)
    for n, candidate in enumerate(shuffled, start=1):
        piid, company, value, ends, office, description, reasons, link, key = candidate
        values = {
            "contract": piid,
            "company": company,
            "value": f"${value:,.0f}" if value is not None else "",
            "ends": ends,
            "office": office,
            "description": description,
            "why": "; ".join(reasons or []),
            "link": link,
            "id": key,
        }
        row |= {f"c{n}_{name}": _text(value) for name, value in values.items()}
    return {column: row.get(column, "") for column in sheet_columns()}


def write_sheet(rows: Iterable[Mapping[str, str]], path: Path) -> int:
    """Write the sheet as UTF-8 with a byte-order mark, so Excel shows accents correctly."""
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sheet_columns())
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


@dataclass(frozen=True)
class Label:
    notice_id: str
    award_key: str  # "" unless the answer names a candidate
    answer: str  # "incumbent" | "other" | "none" | "unsure"
    source: str
    labeled_at: str
    notes: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "notice_id": self.notice_id,
            "award_key": self.award_key,
            "answer": self.answer,
            "source": self.source,
            "labeled_at": self.labeled_at,
            "notes": self.notes,
        }


class LabelError(ValueError):
    pass


def read_answers(path: Path, source: str, today: date) -> list[Label]:
    """Answers from a filled-in sheet: a candidate number (1-5), `other` (the incumbent is
    not listed), `none` (no incumbent: new work) or `unsure`/blank."""
    labels: list[Label] = []
    problems: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            notice_id = (row.get("notice_id") or "").strip()
            raw = (row.get("answer") or "").strip().lower()
            notes = (row.get("notes") or "").strip()
            if not notice_id:
                continue
            if raw.isdigit():
                key = (row.get(f"c{raw}_id") or "").strip()
                if not key:
                    problems.append(f"line {line}: answer {raw} but candidate {raw} is empty")
                    continue
                labels.append(Label(notice_id, key, "incumbent", source, today.isoformat(), notes))
            elif raw in ANSWERS:
                answer = ANSWERS[raw]
                labels.append(Label(notice_id, "", answer, source, today.isoformat(), notes))
            else:
                problems.append(f"line {line}: '{raw}' is not 1-5, other, none or unsure")
    if problems:
        raise LabelError("The sheet has answers I can't read:\n" + "\n".join(problems))
    return labels


def load_labels(path: Path = LABELS_FILE) -> list[Label]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            Label(
                notice_id=row["notice_id"],
                award_key=row["award_key"],
                answer=row["answer"],
                source=row["source"],
                labeled_at=row["labeled_at"],
                notes=row.get("notes", ""),
            )
            for row in csv.DictReader(handle)
        ]


def merge_labels(existing: Sequence[Label], new: Sequence[Label]) -> list[Label]:
    """New answers replace older ones for the same notice; output is sorted by notice."""
    merged = {label.notice_id: label for label in existing}
    merged |= {label.notice_id: label for label in new}
    return [merged[key] for key in sorted(merged)]


def save_labels(labels: Iterable[Label], path: Path = LABELS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(label.as_row() for label in labels)


def _text(value: Any) -> str:
    return "" if value is None else str(value)
