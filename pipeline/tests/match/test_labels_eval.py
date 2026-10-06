"""The labeling spreadsheet round trip and the evaluation metrics."""

import csv
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from pipeline.match.eval import (
    Candidate,
    automatic_decision,
    evaluate,
    explicit_check,
    render,
    score_labels,
)
from pipeline.match.labels import (
    Label,
    LabelError,
    label_sheet,
    load_labels,
    merge_labels,
    read_answers,
    save_labels,
    sheet_columns,
    write_sheet,
)
from pipeline.tests.match.conftest import CFG, run_matcher

Conn = psycopg.Connection[TupleRow]
TODAY = date(2026, 10, 7)


def label(notice: str, award: str = "", answer: str = "incumbent") -> Label:
    return Label(notice, award, answer, "test", "2026-10-07")


CANDIDATES = {
    "clear": [Candidate("A", 0.95, 0.95, "candidate"), Candidate("B", 0.5, 0.5, "candidate")],
    "close": [Candidate("C", 0.9, 0.9, "candidate"), Candidate("D", 0.85, 0.85, "candidate")],
    "cited": [
        Candidate("E", 0.97, 0.7, "explicit_reference"),
        Candidate("F", 0.6, 0.6, "candidate"),
    ],
}


def test_automatic_decision_mirrors_the_threshold_and_margin_rule() -> None:
    assert automatic_decision(CANDIDATES["clear"], 0.8, CFG).incumbent == "A"
    close = automatic_decision(CANDIDATES["close"], 0.8, CFG)
    assert (close.incumbent, close.possible) == (None, ("C", "D"))
    assert automatic_decision([], 0.8, CFG).incumbent is None


def test_precision_recall_and_possible_hit_rate() -> None:
    labels = [
        label("clear", "A"),  # shown and right
        label("cited", "F"),  # shown (E) but wrong
        label("close", "D"),  # not shown; D is among the possible incumbents
        label("none-notice", answer="none"),
        label("skip", answer="unsure"),
    ]
    card = score_labels(labels, CANDIDATES, 0.8, CFG)
    assert (card.labeled, card.shown, card.correct) == (4, 2, 1)
    assert card.precision == 0.5
    assert (card.with_incumbent, card.found) == (3, 1)
    assert (card.possible_cases, card.possible_hits) == (1, 1)
    stricter = score_labels(labels, CANDIDATES, 0.96, CFG)
    assert (stricter.shown, stricter.correct) == (1, 0)


def test_explicit_check_ranks_without_the_citation() -> None:
    check = explicit_check(CANDIDATES, CFG)
    assert (check.notices, check.ranked_first, check.would_show) == (1, 1, 0)


def test_labels_merge_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    save_labels(merge_labels([label("n1", "A"), label("n2", "B")], [label("n1", "C")]), path)
    assert [(x.notice_id, x.award_key) for x in load_labels(path)] == [("n1", "C"), ("n2", "B")]
    assert load_labels(tmp_path / "missing.csv") == []


def fill(sheet: Path, answers: dict[str, str]) -> Path:
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["answer"] = answers.get(row["notice_id"], "")
    filled = sheet.with_name("filled.csv")
    with filled.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sheet_columns())
        writer.writeheader()
        writer.writerows(rows)
    return filled


def candidate_number(sheet: Path, notice_id: str, award_key: str) -> str:
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        row = next(r for r in csv.DictReader(handle) if r["notice_id"] == notice_id)
    return next(str(n) for n in range(1, 6) if row[f"c{n}_id"] == award_key)


@pytest.mark.db
def test_sheet_to_labels_to_report(db_url: str, world: Conn, tmp_path: Path) -> None:
    run_matcher(db_url, tmp_path)
    rows = label_sheet(world, 10)
    assert {row["notice_id"] for row in rows} == {"N1", "N2"}
    n1 = next(row for row in rows if row["notice_id"] == "N1")
    assert {n1[f"c{n}_id"] for n in range(1, 4)} == {"A1", "A2", "A4"}
    assert not any("score" in column or "shown" in column for column in n1)  # no hints
    assert label_sheet(world, 10) == rows  # repeatable

    sheet = tmp_path / "sheet.csv"
    assert write_sheet(rows, sheet) == 2
    filled = fill(sheet, {"N1": candidate_number(sheet, "N1", "A1"), "N2": "other"})
    labels = read_answers(filled, "spreadsheet", TODAY)
    assert [(x.notice_id, x.award_key, x.answer) for x in labels] == [
        ("N1", "A1", "incumbent"),
        ("N2", "", "other"),
    ]

    report = evaluate(world, labels, CFG, TODAY)
    # N1's incumbent is right; N2's shown incumbent (the cited contract) is not the
    # labeler's answer, so precision is 1 of 2.
    assert (report.card.shown, report.card.correct) == (2, 1)
    text = render(report)
    assert "**50%** (1 of 2)" in text and "does not meet" in text
    assert "| 0.80 | 2 | 50% | 50% |" in text


@pytest.mark.db
def test_admin_confirmations_count_as_labels(db_url: str, world: Conn, tmp_path: Path) -> None:
    run_matcher(db_url, tmp_path)
    world.execute(
        """
        INSERT INTO match_reviews (notice_id, award_key, decision)
        VALUES ('N1', 'A1', 'confirmed'), ('N2', 'W91QUZ21C0001', 'rejected')
        """
    )
    report = evaluate(world, [], CFG, TODAY)
    assert (report.review_labels, report.card.labeled, report.card.correct) == (1, 1, 1)
    assert "meets" in render(report)


def test_unreadable_answers_are_reported(tmp_path: Path) -> None:
    sheet = tmp_path / "bad.csv"
    sheet.write_text("notice_id,c1_id,answer\nN1,,1\nN2,A,maybe\nN3,A,1\n", encoding="utf-8-sig")
    with pytest.raises(LabelError) as error:
        read_answers(sheet, "spreadsheet", TODAY)
    assert "line 2: answer 1 but candidate 1 is empty" in str(error.value)
    assert "line 3: 'maybe' is not 1-5, other, none or unsure" in str(error.value)


@pytest.mark.db
def test_report_without_labels_says_so(conn: Conn) -> None:
    text = render(evaluate(conn, [], CFG, TODAY))
    assert "No labels yet" in text
