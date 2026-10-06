"""Score candidates, rank them deterministically, decide what the app shows, and explain
each match in plain English."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pipeline.match.config import MatchingConfig
from pipeline.match.features import (
    AwardFacts,
    NoticeFacts,
    NoticeSignals,
    candidate_window,
    end_date_feature,
    naics_feature,
    office_feature,
    psc_feature,
    set_aside_family,
    set_aside_feature,
    text_feature,
    value_feature,
    vehicle_feature,
)

Shown = Literal["incumbent", "possible", "hidden"]


@dataclass(frozen=True)
class ExplicitHit:
    """The notice text cites this award's contract number."""

    cited_as: str
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scored:
    award: AwardFacts
    features: dict[str, float]
    base_score: float
    score: float
    explicit: ExplicitHit | None
    shared_terms: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def method(self) -> str:
        return "explicit_reference" if self.explicit else "candidate"

    def evidence(self, notice: NoticeFacts) -> dict[str, Any]:
        """JSON-safe evidence stored with the match."""
        evidence: dict[str, Any] = {
            "reasons": list(self.reasons),
            "features": {name: round(value, 4) for name, value in self.features.items()},
            "base_score": round(self.base_score, 4),
            "reference_date": notice.reference_date.isoformat(),
            "end_date": self.award.end_date.isoformat() if self.award.end_date else None,
            "shared_terms": list(self.shared_terms),
        }
        if self.explicit:
            evidence["cited_as"] = self.explicit.cited_as
            evidence["context_words"] = list(self.explicit.context)
        return evidence


def score_candidate(
    notice: NoticeFacts,
    signals: NoticeSignals,
    award: AwardFacts,
    similarity: float,
    shared: tuple[str, ...],
    cfg: MatchingConfig,
    explicit: ExplicitHit | None = None,
) -> Scored:
    window = candidate_window(notice.reference_date, cfg.candidates)
    f = cfg.features
    features = {
        "office": office_feature(notice, award, f),
        "text": text_feature(similarity, f),
        "naics": naics_feature(notice.naics_codes, award.naics, f),
        "end_date": end_date_feature(notice.reference_date, award.end_date, window, f),
        "psc": psc_feature(notice.psc, award.psc, f),
        "set_aside": set_aside_feature(notice.set_aside_code, award.set_aside_code),
        "value": value_feature(signals.stated_values, award.total_value, f),
        "vehicle": vehicle_feature(signals.vehicles, award, f),
    }
    weights = cfg.weights.model_dump()
    base = sum(weights[name] * value for name, value in features.items())
    score = max(base, cfg.explicit_reference.score) if explicit else base
    reasons = explain(notice, award, features, shared, explicit)
    return Scored(award, features, base, min(score, 1.0), explicit, shared, reasons)


def rank(scored: Sequence[Scored], reference: date) -> list[Scored]:
    """Highest score first. Ties: an explicit reference, then the same office, then the
    end date nearest the reference date, then the larger value, then award_key."""

    def key(item: Scored) -> tuple[float, int, float, int, float, str]:
        end = item.award.end_date
        distance = abs((end - reference).days) if end else 10**6
        return (
            -round(item.score, 6),
            0 if item.explicit else 1,
            -item.features["office"],
            distance,
            -(item.award.total_value or 0.0),
            item.award.award_key,
        )

    return sorted(scored, key=key)


def decide(
    ranked: Sequence[Scored], human: Mapping[str, str], cfg: MatchingConfig
) -> dict[str, Shown]:
    """What the app shows for each automatic match of one notice.

    A human-confirmed match wins outright (it is shown from its own row), so every
    automatic one is hidden. Otherwise the top match is the incumbent only if it clears
    the threshold and beats the runner-up by the margin; if not, up to `max_possible`
    matches above the floor are shown as possible incumbents."""
    shown: dict[str, Shown] = {item.award.award_key: "hidden" for item in ranked}
    if "confirmed" in human.values():
        return shown
    pool = [item for item in ranked if human.get(item.award.award_key) != "rejected"]
    if not pool:
        return shown
    d = cfg.decision
    top = pool[0]
    runner_up = pool[1].score if len(pool) > 1 else 0.0
    if top.score >= d.incumbent_threshold and top.score - runner_up >= d.margin:
        shown[top.award.award_key] = "incumbent"
        return shown
    for item in pool[: d.max_possible]:
        if item.score >= d.possible_floor:
            shown[item.award.award_key] = "possible"
    return shown


def explain(
    notice: NoticeFacts,
    award: AwardFacts,
    features: Mapping[str, float],
    shared: tuple[str, ...],
    explicit: ExplicitHit | None,
) -> tuple[str, ...]:
    """Plain-English reasons, strongest evidence first. Only supporting facts are listed."""
    reasons: list[str] = []
    piid = award.piid or award.award_key
    if explicit:
        reason = f"The notice mentions this contract ({explicit.cited_as})"
        if explicit.context:
            reason += f" near the words “{explicit.context[0]}”"
        reasons.append(reason)
    if features["office"] == 1.0:
        reasons.append(f"Same contracting office ({award.office_name or award.office_code})")
    elif features["office"] > 0:
        agency = award.subtier_name or award.subtier_code
        reasons.append(f"Same agency ({agency}), different office")
    if features["end_date"] > 0 and award.end_date:
        reasons.append(_end_date_reason(notice, award.end_date))
    if features["naics"] == 1.0:
        reasons.append(f"Same industry code (NAICS {award.naics})")
    elif features["naics"] > 0:
        reasons.append(f"Related industry code (NAICS {award.naics})")
    if features["text"] >= 0.3 and shared:
        reasons.append("Similar work: " + ", ".join(shared))
    if features["psc"] == 1.0 and award.psc:
        reasons.append(f"Same product or service code ({award.psc})")
    if features["vehicle"] == 1.0:
        reasons.append("Ordered under the same contract vehicle the notice names")
    if features["set_aside"] == 1.0 and set_aside_family(award.set_aside_code) != "none":
        reasons.append(f"Same set-aside ({award.set_aside or award.set_aside_code})")
    if features["value"] == 1.0:
        reasons.append("Contract value is in line with the amount in the notice")
    if not reasons:
        reasons.append(f"Contract {piid} is from the same agency")
    return tuple(reasons)


def _end_date_reason(notice: NoticeFacts, end: date) -> str:
    when = end.strftime("%b %Y")
    anchor = "the response deadline" if notice.has_deadline else "the notice date"
    months = round((end - notice.reference_date).days / 30.44)
    if months == 0:
        return f"Current contract ends {when}, around {anchor}"
    side = "after" if months > 0 else "before"
    unit = "month" if abs(months) == 1 else "months"
    return f"Current contract ends {when}, {abs(months)} {unit} {side} {anchor}"
