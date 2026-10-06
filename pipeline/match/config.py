"""Matcher settings from `pipeline/config/matching.yaml`, validated."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MATCHING_FILE = Path(__file__).resolve().parent.parent / "config" / "matching.yaml"

Unit = Annotated[float, Field(ge=0, le=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Candidates(_Frozen):
    months_before: int = Field(ge=0)
    months_after: int = Field(ge=0)
    award_types: tuple[str, ...]
    vehicle_award_types: tuple[str, ...]
    min_total_value: float = Field(ge=0)
    max_per_notice: int = Field(ge=1)


class Weights(_Frozen):
    office: Unit
    text: Unit
    naics: Unit
    end_date: Unit
    psc: Unit
    set_aside: Unit
    value: Unit
    vehicle: Unit

    @model_validator(mode="after")
    def _sum_to_one(self) -> "Weights":
        total = sum(self.model_dump().values())
        if abs(total - 1) > 1e-6:
            raise ValueError(f"weights must add up to 1 (they add up to {total:.4f})")
        return self


class Features(_Frozen):
    office_same_subtier_only: Unit
    naics_same_group: Unit
    psc_same_prefix2: Unit
    psc_same_prefix1: Unit
    psc_missing: Unit
    value_missing: Unit
    vehicle_missing: Unit
    text_full_at: float = Field(gt=0, le=1)
    end_date_ideal_from_days: int
    end_date_ideal_to_days: int


class ExplicitReference(_Frozen):
    score: Unit
    context_words: tuple[str, ...]
    context_chars: int = Field(ge=0)


class Decision(_Frozen):
    incumbent_threshold: Unit
    margin: Unit
    possible_floor: Unit
    max_possible: int = Field(ge=0)
    store_floor: Unit
    store_max: int = Field(ge=1)


class MatchingConfig(_Frozen):
    matcher_version: str
    notice_types: tuple[str, ...]
    days_after_posting_without_deadline: int = Field(ge=0)
    candidates: Candidates
    weights: Weights
    features: Features
    explicit_reference: ExplicitReference
    decision: Decision

    @model_validator(mode="after")
    def _consistent(self) -> "MatchingConfig":
        f = self.features
        window_from = -self.candidates.months_before * 31
        window_to = self.candidates.months_after * 31
        if not window_from <= f.end_date_ideal_from_days <= f.end_date_ideal_to_days <= window_to:
            raise ValueError("the ideal end-date range must sit inside the candidate window")
        d = self.decision
        if not d.store_floor <= d.possible_floor <= d.incumbent_threshold:
            raise ValueError("expected store_floor <= possible_floor <= incumbent_threshold")
        if set(self.candidates.award_types) & set(self.candidates.vehicle_award_types):
            raise ValueError("an award type cannot be both a candidate and a vehicle")
        return self


def load_matching_config(path: Path = MATCHING_FILE) -> MatchingConfig:
    return MatchingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
