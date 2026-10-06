"""`app match eval`: how often is the incumbent the app shows the right one?

Labels come from the labeling spreadsheet (`labeled_matches.csv`) and from admin
decisions (`match_reviews`; the spreadsheet wins for the same notice). The matcher's own
decision is rebuilt from the stored scores, ignoring human statuses, so confirming a
match in the admin queue does not inflate the result.

- Precision: of the notices where an incumbent is shown, how many show the right one.
- Recall: of the labeled notices that have an incumbent, how many show it.
- Possible hit rate: when no incumbent is shown, how often the right one is among the
  possible incumbents.
- Threshold sweep: the same numbers at other thresholds, for tuning.
- Automatic check: for notices that cite a contract number, would the other features
  alone have ranked the cited contract first?"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from pipeline.match.candidates import Conn
from pipeline.match.config import MatchingConfig
from pipeline.match.labels import Label

TARGET_PRECISION = 0.9
SWEEP = (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)


@dataclass(frozen=True)
class Candidate:
    award_key: str
    score: float
    base_score: float
    method: str


@dataclass(frozen=True)
class Decision:
    incumbent: str | None
    possible: tuple[str, ...]


def automatic_decision(
    candidates: Sequence[Candidate], threshold: float, cfg: MatchingConfig
) -> Decision:
    """The show/hide rule of `scoring.decide`, applied to stored scores."""
    ranked = sorted(candidates, key=lambda c: (-c.score, c.award_key))
    if not ranked:
        return Decision(None, ())
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if ranked[0].score >= threshold and ranked[0].score - runner_up >= cfg.decision.margin:
        return Decision(ranked[0].award_key, ())
    d = cfg.decision
    possible = tuple(c.award_key for c in ranked[: d.max_possible] if c.score >= d.possible_floor)
    return Decision(None, possible)


@dataclass
class Scorecard:
    labeled: int = 0
    shown: int = 0
    correct: int = 0
    with_incumbent: int = 0
    found: int = 0
    possible_cases: int = 0
    possible_hits: int = 0

    @property
    def precision(self) -> float | None:
        return self.correct / self.shown if self.shown else None

    @property
    def recall(self) -> float | None:
        return self.found / self.with_incumbent if self.with_incumbent else None

    @property
    def possible_rate(self) -> float | None:
        return self.possible_hits / self.possible_cases if self.possible_cases else None


def score_labels(
    labels: Iterable[Label],
    candidates: dict[str, list[Candidate]],
    threshold: float,
    cfg: MatchingConfig,
) -> Scorecard:
    card = Scorecard()
    for label in labels:
        if label.answer == "unsure":
            continue
        card.labeled += 1
        decision = automatic_decision(candidates.get(label.notice_id, []), threshold, cfg)
        truth = label.award_key if label.answer == "incumbent" else None
        has_incumbent = label.answer in ("incumbent", "other")
        if decision.incumbent is not None:
            card.shown += 1
            card.correct += decision.incumbent == truth
        if has_incumbent:
            card.with_incumbent += 1
            card.found += decision.incumbent is not None and decision.incumbent == truth
            if decision.incumbent is None and decision.possible and truth:
                card.possible_cases += 1
                card.possible_hits += truth in decision.possible
    return card


@dataclass
class ExplicitCheck:
    notices: int = 0
    ranked_first: int = 0
    would_show: int = 0


def explicit_check(candidates: dict[str, list[Candidate]], cfg: MatchingConfig) -> ExplicitCheck:
    """Re-rank each citing notice's candidates by their scores without the citation."""
    check = ExplicitCheck()
    for rows in candidates.values():
        cited = [c for c in rows if c.method == "explicit_reference"]
        if len(cited) != 1:
            continue
        check.notices += 1
        masked = [Candidate(c.award_key, c.base_score, c.base_score, c.method) for c in rows]
        ranked = sorted(masked, key=lambda c: (-c.score, c.award_key))
        check.ranked_first += ranked[0].award_key == cited[0].award_key
        decision = automatic_decision(masked, cfg.decision.incumbent_threshold, cfg)
        check.would_show += decision.incumbent == cited[0].award_key
    return check


def load_candidates(
    conn: Conn, notice_ids: Iterable[str] | None = None
) -> dict[str, list[Candidate]]:
    """Stored incumbent candidates with their scores (any status), per notice."""
    ids = None if notice_ids is None else sorted(set(notice_ids))
    rows = conn.execute(
        """
        SELECT notice_id, award_key, score::float8,
               coalesce((evidence ->> 'base_score')::float8, score::float8), method
        FROM notice_award_matches
        WHERE kind = 'incumbent' AND (%(ids)s::text[] IS NULL OR notice_id = ANY(%(ids)s))
        """,
        {"ids": ids},
    ).fetchall()
    result: dict[str, list[Candidate]] = defaultdict(list)
    for notice_id, award_key, score, base, method in rows:
        result[notice_id].append(Candidate(award_key, score, base, method))
    return dict(result)


def labels_from_reviews(conn: Conn) -> list[Label]:
    """Admin confirmations as labels (the latest decision per notice and award)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (notice_id, award_key) notice_id, award_key, decision,
               created_at::date, coalesce(note, '')
        FROM match_reviews ORDER BY notice_id, award_key, created_at DESC, id DESC
        """
    ).fetchall()
    return [
        Label(notice_id, award_key, "incumbent", "admin_review", day.isoformat(), note)
        for notice_id, award_key, decision, day, note in rows
        if decision == "confirmed"
    ]


@dataclass
class Report:
    today: date
    cfg: MatchingConfig
    sheet_labels: int
    review_labels: int
    card: Scorecard
    sweep: list[tuple[float, Scorecard]] = field(default_factory=list)
    explicit: ExplicitCheck = field(default_factory=ExplicitCheck)

    @property
    def meets_target(self) -> bool:
        return self.card.precision is not None and self.card.precision >= TARGET_PRECISION


def evaluate(conn: Conn, sheet: Sequence[Label], cfg: MatchingConfig, today: date) -> Report:
    reviews = labels_from_reviews(conn)
    labeled = {label.notice_id: label for label in reviews}
    labeled |= {label.notice_id: label for label in sheet}
    candidates = load_candidates(conn)
    threshold = cfg.decision.incumbent_threshold
    return Report(
        today=today,
        cfg=cfg,
        sheet_labels=len(sheet),
        review_labels=len(reviews),
        card=score_labels(labeled.values(), candidates, threshold, cfg),
        sweep=[(t, score_labels(labeled.values(), candidates, t, cfg)) for t in SWEEP],
        explicit=explicit_check(candidates, cfg),
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def render(report: Report) -> str:
    cfg, card = report.cfg, report.card
    lines = [
        "# Incumbent matching: evaluation",
        "",
        f"Generated {report.today} by `uv run app match eval`. Matcher {cfg.matcher_version}, "
        f"threshold {cfg.decision.incumbent_threshold}, margin {cfg.decision.margin}.",
        "",
        f"Labels: {report.sheet_labels} from the labeling spreadsheet, "
        f"{report.review_labels} admin confirmations (the spreadsheet wins for the same "
        "notice). Answers marked “unsure” are left out.",
        "",
        "## Result",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Labeled notices | {card.labeled} |",
        f"| Incumbent shown | {card.shown} |",
        f"| **Precision of shown incumbents** (target ≥ {TARGET_PRECISION:.0%}) | "
        f"**{_pct(card.precision)}** ({card.correct} of {card.shown}) |",
        f"| Recall (labeled notices with an incumbent) | {_pct(card.recall)} "
        f"({card.found} of {card.with_incumbent}) |",
        f"| Right one among possible incumbents, when none is shown | "
        f"{_pct(card.possible_rate)} ({card.possible_hits} of {card.possible_cases}) |",
        "",
    ]
    if card.labeled == 0:
        lines += ["No labels yet: fill in the labeling spreadsheet (`app match label-sheet`).", ""]
    else:
        verdict = "meets" if report.meets_target else "does not meet"
        lines += [f"The matcher **{verdict}** the precision target.", ""]
    lines += [
        "## Threshold sweep",
        "",
        "| Threshold | Shown | Precision | Recall |",
        "|---|---|---|---|",
    ]
    for threshold, sweep in report.sweep:
        lines.append(
            f"| {threshold:.2f} | {sweep.shown} | {_pct(sweep.precision)} | {_pct(sweep.recall)} |"
        )
    check = report.explicit
    lines += [
        "",
        "## Automatic check: notices that cite a contract number",
        "",
        "A notice that names the contract it replaces is a free label. With that citation "
        "hidden, the other features alone:",
        "",
        f"- rank the cited contract first in {check.ranked_first} of {check.notices} notices;",
        f"- would show it as the incumbent in {check.would_show} of {check.notices}.",
        "",
    ]
    return "\n".join(lines)
