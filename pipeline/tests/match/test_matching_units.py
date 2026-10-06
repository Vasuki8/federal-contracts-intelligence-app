"""Matcher building blocks: contract-number extraction, text similarity, features,
scoring, ranking, the show/hide decision and config validation."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pipeline.match.config import MATCHING_FILE, load_matching_config
from pipeline.match.features import (
    AwardFacts,
    NoticeFacts,
    NoticeSignals,
    add_months,
    candidate_window,
    end_date_feature,
    naics_feature,
    psc_feature,
    set_aside_feature,
    value_feature,
)
from pipeline.match.piids import (
    context_words,
    find_contract_numbers,
    looks_like_contract_number,
    stated_amounts,
)
from pipeline.match.scoring import ExplicitHit, decide, rank, score_candidate
from pipeline.match.text import TextModel, cosine, shared_terms, tokens

CFG = load_matching_config()
F = CFG.features
REF = date(2026, 12, 1)

NOTICE = NoticeFacts(
    notice_id="n1",
    title="IT Help Desk Support Services",
    description=None,
    solicitation_number="47QTCA-26-Q-0007",
    subtier_code="4732",
    office_code="47QTCA",
    naics_codes=("541512",),
    psc="DA01",
    set_aside_code="SBA",
    reference_date=REF,
    has_deadline=True,
)


def award(key: str, **changes: object) -> AwardFacts:
    base = AwardFacts(
        award_key=key,
        piid=key,
        piid_norm=key,
        award_type_code="C",
        subtier_code="4732",
        subtier_name="FEDERAL ACQUISITION SERVICE",
        office_code="47QTCA",
        office_name="GSA/FAS IT CENTER",
        naics="541512",
        psc="DA01",
        set_aside_code="SBA",
        set_aside="SMALL BUSINESS SET ASIDE - TOTAL",
        total_value=1_000_000.0,
        end_date=date(2027, 1, 15),
        description="HELP DESK SUPPORT TASK ORDER",
        recipient_uei="UEI1",
        recipient_name="ACME",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# --- contract numbers -------------------------------------------------------


def test_contract_numbers_are_found_in_dod_and_civilian_formats() -> None:
    text = (
        "Follow-on to incumbent contract W91QUZ-21-C-0001 (prior order 47QTCA20F0001) "
        "under GS-35F-0119Y. Call 703-555-1234 by 2026-10-05; ref 8246dc8caad6468b90d37254bf50edcf"
    )
    found = [c.piid_norm for c in find_contract_numbers(text)]
    assert found == ["W91QUZ21C0001", "47QTCA20F0001", "GS35F0119Y"]


def test_own_solicitation_and_duplicates_are_excluded() -> None:
    text = "Solicitation 47QTCA-26-Q-0007 replaces 47QTCA20F0001; see 47QTCA20F0001."
    found = find_contract_numbers(text, exclude=["47QTCA 26 Q 0007"])
    assert [c.piid_norm for c in found] == ["47QTCA20F0001"]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("W91QUZ21C0001", True),
        ("GS35F0119Y", True),
        ("ABCDEFGH1", False),  # one digit
        ("20261005", False),  # no letter
        ("A1234", False),  # too short
        ("8246DC8CAAD6468B90D37254BF50EDCF", False),  # too long (a notice id)
    ],
)
def test_contract_number_shape(token: str, expected: bool) -> None:
    assert looks_like_contract_number(token) is expected


def test_context_words_near_a_citation() -> None:
    text = "This is a follow-on to the incumbent contract W91QUZ21C0001."
    citation = find_contract_numbers(text)[0]
    assert context_words(text, citation, ["incumbent", "bridge"], 60) == ("incumbent",)


def test_stated_amounts() -> None:
    text = "Estimated value $2.5 million; prior ceiling $1,250,000; fee $500; budget $3M."
    assert stated_amounts(text) == (2_500_000.0, 1_250_000.0, 3_000_000.0)


# --- text ---------------------------------------------------------------------


def test_tokens_drop_boilerplate_numbers_and_stopwords() -> None:
    assert tokens("IGF::OT::IGF Help Desk support for the FY24 2024 base") == [
        "help",
        "desk",
        "support",
        "fy24",
        "base",
    ]


def test_tfidf_prefers_rare_shared_words() -> None:
    model = TextModel.from_texts(
        ["help desk support", "network support", "cloud support", "help desk tier 2"]
    )
    notice = model.vector("IT help desk support services")
    help_desk = model.vector("HELP DESK SUPPORT TASK ORDER")
    network = model.vector("NETWORK SUPPORT")
    assert cosine(notice, help_desk) > cosine(notice, network) > 0
    assert shared_terms(notice, help_desk) == ("desk", "help", "support")
    assert cosine(model.vector(""), help_desk) == 0


# --- features -----------------------------------------------------------------


def test_add_months_clamps_to_month_end() -> None:
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months(date(2026, 1, 15), -6) == date(2025, 7, 15)


def test_end_date_curve() -> None:
    window = candidate_window(REF, CFG.candidates)
    assert window == (date(2026, 6, 1), date(2028, 6, 1))
    assert end_date_feature(REF, date(2027, 3, 1), window, F) == 1.0  # ideal range
    assert end_date_feature(REF, date(2026, 6, 1), window, F) == 0.0  # window edge
    assert end_date_feature(REF, date(2028, 6, 2), window, F) == 0.0  # outside
    early = end_date_feature(REF, date(2026, 9, 1), window, F)
    late = end_date_feature(REF, date(2027, 12, 1), window, F)
    assert 0 < early < 1 and 0 < late < 1
    assert end_date_feature(REF, None, window, F) == 0.0


def test_naics_psc_and_set_aside_relatedness() -> None:
    assert naics_feature(("541512",), "541512", F) == 1.0
    assert naics_feature(("541512",), "541519", F) == F.naics_same_group
    assert naics_feature(("541512",), "541330", F) == 0.0
    assert psc_feature("DA01", "DA01", F) == 1.0
    assert psc_feature("DA01", "DA10", F) == F.psc_same_prefix2
    assert psc_feature("DA01", "DB10", F) == F.psc_same_prefix1
    assert psc_feature(None, "DA01", F) == F.psc_missing
    assert set_aside_feature("8A", "8AN") == 1.0
    assert set_aside_feature("SBA", "HZC") == 0.5
    assert set_aside_feature(None, "NONE") == 1.0
    assert set_aside_feature("SBA", "NONE") == 0.0


def test_value_consistency() -> None:
    assert value_feature((), 1e6, F) == F.value_missing
    assert value_feature((2e6,), 1e6, F) == 1.0
    assert value_feature((2e7,), 1e6, F) == 0.2


# --- scoring ------------------------------------------------------------------


def scored(key: str, similarity: float = 0.4, explicit: bool = False, **changes: object):  # type: ignore[no-untyped-def]
    hit = ExplicitHit(cited_as=key, context=("incumbent",)) if explicit else None
    return score_candidate(
        NOTICE, NoticeSignals(), award(key, **changes), similarity, ("help", "desk"), CFG, hit
    )


def test_strong_candidate_scores_high_with_plain_english_reasons() -> None:
    item = scored("A1")
    assert item.score > 0.8
    assert item.reasons[0] == "Same contracting office (GSA/FAS IT CENTER)"
    assert "Current contract ends Jan 2027, 1 month after the response deadline" in item.reasons
    assert "Similar work: help, desk" in item.reasons
    assert item.method == "candidate"


def test_explicit_reference_overrides_the_score() -> None:
    item = scored("A1", similarity=0.0, explicit=True, office_code="OTHER", naics="111111")
    assert item.base_score < 0.6
    assert item.score == CFG.explicit_reference.score
    assert item.reasons[0] == "The notice mentions this contract (A1) near the words “incumbent”"
    assert item.method == "explicit_reference"


def test_rank_is_deterministic_on_ties() -> None:
    a = scored("B", total_value=1e6)
    b = scored("A", total_value=1e6)
    c = scored("C", total_value=5e6)
    d = scored("D", end_date=date(2027, 6, 1))  # same score, end date further away
    assert a.score == b.score == c.score == d.score
    assert [s.award.award_key for s in rank([a, d, b, c], REF)] == ["C", "A", "B", "D"]


def test_decision_threshold_and_margin() -> None:
    strong = scored("A", similarity=0.5)
    weak = scored(
        "B", similarity=0.0, office_code="X", naics="541330", psc="R499", end_date=date(2026, 7, 1)
    )
    shown = decide(rank([strong, weak], REF), {}, CFG)
    assert shown == {"A": "incumbent", "B": "hidden"}

    twin = scored("C", similarity=0.5)  # as strong as A: no clear winner
    shown = decide(rank([strong, twin, weak], REF), {}, CFG)
    assert shown == {"A": "possible", "C": "possible", "B": "hidden"}


def test_decision_respects_human_reviews() -> None:
    strong, other = scored("A", similarity=0.5), scored("B", similarity=0.1)
    ranked = rank([strong, other], REF)
    assert decide(ranked, {"Z": "confirmed"}, CFG) == {"A": "hidden", "B": "hidden"}
    # A rejected top match is skipped; the next one competes on its own.
    assert decide(ranked, {"A": "rejected"}, CFG)["B"] in {"incumbent", "possible"}
    assert decide(ranked, {"A": "rejected"}, CFG)["A"] == "hidden"


def test_evidence_is_json_safe() -> None:
    evidence = scored("A1", explicit=True).evidence(NOTICE)
    assert evidence["cited_as"] == "A1"
    assert evidence["end_date"] == "2027-01-15"
    assert set(evidence["features"]) == set(CFG.weights.model_dump())


# --- config -------------------------------------------------------------------


def write_config(tmp_path: Path, **override: object) -> Path:
    data = yaml.safe_load(MATCHING_FILE.read_text())
    for dotted, value in override.items():
        section, key = dotted.split("__")
        data[section][key] = value
    path = tmp_path / "matching.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_shipped_config_is_valid() -> None:
    assert abs(sum(CFG.weights.model_dump().values()) - 1) < 1e-9


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"weights__office": 0.5}, "add up to 1"),
        ({"decision__possible_floor": 0.9}, "store_floor <= possible_floor"),
        ({"candidates__vehicle_award_types": ["C"]}, "both a candidate and a vehicle"),
        ({"features__end_date_ideal_to_days": 9999}, "inside the candidate window"),
    ],
)
def test_invalid_config_is_rejected(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        load_matching_config(write_config(tmp_path, **override))
