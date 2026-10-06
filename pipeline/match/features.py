"""The facts the matcher compares, and one function per feature (each returns 0..1)."""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from pipeline.match.config import Candidates, Features


@dataclass(frozen=True)
class NoticeFacts:
    notice_id: str
    title: str | None
    description: str | None
    solicitation_number: str | None
    subtier_code: str | None
    office_code: str | None
    naics_codes: tuple[str, ...]
    psc: str | None
    set_aside_code: str | None
    reference_date: date
    has_deadline: bool

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.title, self.description) if part)


@dataclass(frozen=True)
class AwardFacts:
    award_key: str
    piid: str | None
    piid_norm: str | None
    award_type_code: str | None
    subtier_code: str | None
    subtier_name: str | None
    office_code: str | None
    office_name: str | None
    naics: str | None
    psc: str | None
    set_aside_code: str | None
    set_aside: str | None
    total_value: float | None
    end_date: date | None
    description: str | None
    recipient_uei: str | None
    recipient_name: str | None
    referenced_idv_piid_norm: str | None = None


@dataclass(frozen=True)
class NoticeSignals:
    """What the notice text says beyond its fields: stated dollar amounts and the
    contract vehicles it cites (normalized PIIDs)."""

    stated_values: tuple[float, ...] = ()
    vehicles: frozenset[str] = field(default_factory=frozenset)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def candidate_window(reference: date, candidates: Candidates) -> tuple[date, date]:
    return (
        add_months(reference, -candidates.months_before),
        add_months(reference, candidates.months_after),
    )


def office_feature(notice: NoticeFacts, award: AwardFacts, cfg: Features) -> float:
    if notice.subtier_code is None or award.subtier_code != notice.subtier_code:
        return 0.0
    if notice.office_code is not None and award.office_code == notice.office_code:
        return 1.0
    return cfg.office_same_subtier_only


def naics_feature(notice_codes: Sequence[str], award_code: str | None, cfg: Features) -> float:
    if not award_code or not notice_codes:
        return 0.0
    if award_code in notice_codes:
        return 1.0
    if any(code[:4] == award_code[:4] for code in notice_codes):
        return cfg.naics_same_group
    return 0.0


def psc_feature(notice_psc: str | None, award_psc: str | None, cfg: Features) -> float:
    if not notice_psc or not award_psc:
        return cfg.psc_missing
    a, b = notice_psc.upper(), award_psc.upper()
    if a == b:
        return 1.0
    if a[:2] == b[:2]:
        return cfg.psc_same_prefix2
    if a[:1] == b[:1]:
        return cfg.psc_same_prefix1
    return 0.0


def end_date_feature(
    reference: date, end: date | None, window: tuple[date, date], cfg: Features
) -> float:
    """1 when the award ends in the ideal range around the reference date, falling
    linearly to 0 at the candidate window's edges."""
    if end is None:
        return 0.0
    offset = (end - reference).days
    low, high = (window[0] - reference).days, (window[1] - reference).days
    ideal_from, ideal_to = cfg.end_date_ideal_from_days, cfg.end_date_ideal_to_days
    if offset < low or offset > high:
        return 0.0
    if offset < ideal_from:
        return (offset - low) / (ideal_from - low) if ideal_from > low else 1.0
    if offset > ideal_to:
        return (high - offset) / (high - ideal_to) if high > ideal_to else 1.0
    return 1.0


# SAM.gov and FPDS share set-aside codes; families group a type's full and sole-source forms.
_SET_ASIDE_FAMILY = {
    "SBA": "small",
    "SBP": "small",
    "RSB": "small",
    "ESB": "small",
    "8A": "8a",
    "8AN": "8a",
    "HZC": "hubzone",
    "HZS": "hubzone",
    "SDVOSBC": "sdvosb",
    "SDVOSBS": "sdvosb",
    "WOSB": "wosb",
    "WOSBSS": "wosb",
    "EDWOSB": "edwosb",
    "EDWOSBSS": "edwosb",
    "VSA": "vosb",
    "VSS": "vosb",
    "ISBEE": "isbee",
    "IEE": "isbee",
    "NONE": "none",
}


def set_aside_family(code: str | None) -> str:
    if not code:
        return "none"
    return _SET_ASIDE_FAMILY.get(code.strip().upper(), code.strip().lower())


def set_aside_feature(notice_code: str | None, award_code: str | None) -> float:
    a, b = set_aside_family(notice_code), set_aside_family(award_code)
    if a == b:
        return 1.0
    if "none" not in (a, b):
        return 0.5  # both set aside for small businesses, different programs
    return 0.0


def value_feature(stated: Sequence[float], total: float | None, cfg: Features) -> float:
    """1 when the award's value is within a factor of 3 of an amount stated in the notice."""
    if not stated or total is None or total <= 0:
        return cfg.value_missing
    return 1.0 if any(1 / 3 <= total / amount <= 3 for amount in stated) else 0.2


def vehicle_feature(vehicles: frozenset[str], award: AwardFacts, cfg: Features) -> float:
    """1 when the award was ordered under a contract vehicle the notice cites."""
    if not vehicles:
        return cfg.vehicle_missing
    return 1.0 if award.referenced_idv_piid_norm in vehicles else 0.0


def text_feature(similarity: float, cfg: Features) -> float:
    return min(1.0, max(0.0, similarity / cfg.text_full_at))
