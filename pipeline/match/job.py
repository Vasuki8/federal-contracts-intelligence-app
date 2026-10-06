"""`app match notices`: link notices to the awards made from them, then score incumbent
candidates for every active vertical notice and save what the app should show.

Everything is recomputed each run from the current data, so the job is idempotent:
unchanged matches are not rewritten, and human decisions are never changed."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from pipeline.ingest.context import IngestContext
from pipeline.match.candidates import (
    award_descriptions,
    awards_by_piid,
    fetch_candidates,
    human_reviews,
    load_notices,
    resulting_awards,
    superseded_awards,
)
from pipeline.match.config import MatchingConfig
from pipeline.match.features import AwardFacts, NoticeFacts, NoticeSignals
from pipeline.match.history import link_history
from pipeline.match.piids import context_words, find_contract_numbers, stated_amounts
from pipeline.match.scoring import ExplicitHit, Scored, Shown, decide, rank, score_candidate
from pipeline.match.store import MatchRow, save_matches
from pipeline.match.text import TextModel, Vector, cosine, shared_terms


@dataclass
class MatchSummary:
    notices: int = 0
    with_candidates: int = 0
    incumbents: int = 0
    possible: int = 0
    history_written: int = 0
    written: int = 0
    deleted: int = 0


@dataclass
class Context:
    """Per-run lookups shared by every notice."""

    model: TextModel
    superseded: Mapping[str, set[str]]
    resulting: Mapping[str, set[str]]
    reviews: Mapping[str, Mapping[str, str]]
    award_vectors: dict[str, Vector] = field(default_factory=dict)


@dataclass
class MatchJob:
    ctx: IngestContext
    cfg: MatchingConfig
    vertical: frozenset[str]
    today: date

    def run(self) -> MatchSummary:
        data, version = self.ctx.data, self.cfg.matcher_version
        summary = MatchSummary()
        summary.history_written, _ = link_history(data, self.vertical, version)
        data.commit()

        notices = load_notices(data, self.vertical, self.cfg, self.today)
        self.ctx.log(f"{len(notices)} active notices in scope; building the text model.")
        context = Context(
            model=TextModel.from_texts(award_descriptions(data, self.vertical)),
            superseded=superseded_awards(data),
            resulting=resulting_awards(data),
            reviews=human_reviews(data),
        )
        rows: list[MatchRow] = []
        for index, notice in enumerate(notices):
            if index % 100 == 0:
                self.ctx.keepalive()
            matches = self.match_notice(notice, context)
            rows.extend(matches)
            summary.with_candidates += bool(matches)
            summary.incumbents += any(row.shown == "incumbent" for row in matches)
            summary.possible += any(row.shown == "possible" for row in matches)
        summary.notices = len(notices)
        summary.written, summary.deleted = save_matches(
            data, [notice.notice_id for notice in notices], rows, version
        )
        data.commit()
        self.ctx.run.counters.rows_in += summary.notices
        self.ctx.run.counters.rows_upserted += summary.written + summary.history_written
        self.ctx.log(
            f"{summary.notices} notices: {summary.incumbents} with an incumbent shown, "
            f"{summary.possible} with possible incumbents, "
            f"{summary.notices - summary.with_candidates} with no candidates. "
            f"{summary.written} matches written, {summary.deleted} removed, "
            f"{summary.history_written} award links written."
        )
        return summary

    def match_notice(self, notice: NoticeFacts, context: Context) -> list[MatchRow]:
        """Score one notice's candidates and return the rows to store."""
        data = self.ctx.data
        reviews = context.reviews.get(notice.notice_id, {})
        exclude = set(context.resulting.get(notice.notice_id, set()))
        exclude |= {key for key, by in context.superseded.items() if by - {notice.notice_id}}
        signals, explicit = self._read_text(notice, exclude)
        pool: dict[str, AwardFacts] = {
            award.award_key: award for award in fetch_candidates(data, notice, self.cfg, exclude)
        }
        pool |= {key: award for key, (award, _) in explicit.items()}
        if not pool:
            return []

        title_vector = context.model.vector(notice.title)
        text_vector = context.model.vector(notice.text)
        scored = []
        for key, award in pool.items():
            vector = context.award_vectors.get(key)
            if vector is None:
                vector = context.award_vectors[key] = context.model.vector(award.description)
            hit = explicit[key][1] if key in explicit else None
            # The title is the most targeted text; a long description dilutes the score.
            similarity, best = max(
                (cosine(title_vector, vector), title_vector),
                (cosine(text_vector, vector), text_vector),
                key=lambda pair: pair[0],
            )
            scored.append(
                score_candidate(
                    notice,
                    signals,
                    award,
                    similarity,
                    shared_terms(best, vector),
                    self.cfg,
                    hit,
                )
            )
        ranked = rank(scored, notice.reference_date)
        shown = decide(ranked, reviews, self.cfg)
        return self._rows(notice, ranked, shown, reviews)

    def _read_text(
        self, notice: NoticeFacts, exclude: set[str]
    ) -> tuple[NoticeSignals, dict[str, tuple[AwardFacts, ExplicitHit]]]:
        """Contract numbers cited in the notice: same-agency non-vehicle awards are
        explicit incumbent references; vehicles (GWACs, Schedules, other agencies' IDVs)
        only tell us what the work is ordered under."""
        text = notice.text
        citations = {
            c.piid_norm: c
            for c in find_contract_numbers(text, exclude=[notice.solicitation_number])
        }
        vehicles: set[str] = set()
        explicit: dict[str, tuple[AwardFacts, ExplicitHit]] = {}
        ref = self.cfg.explicit_reference
        for award in awards_by_piid(self.ctx.data, citations):
            if award.award_key in exclude or award.piid_norm is None:
                continue
            kind = award.award_type_code or ""
            same_agency = award.subtier_code == notice.subtier_code
            if kind in self.cfg.candidates.vehicle_award_types or (
                kind.startswith("IDV") and not same_agency
            ):
                vehicles.add(award.piid_norm)
            elif same_agency:
                citation = citations[award.piid_norm]
                words = context_words(text, citation, ref.context_words, ref.context_chars)
                explicit[award.award_key] = (award, ExplicitHit(citation.raw, words))
        return NoticeSignals(stated_amounts(text), frozenset(vehicles)), explicit

    def _rows(
        self,
        notice: NoticeFacts,
        ranked: list[Scored],
        shown: Mapping[str, Shown],
        reviews: Mapping[str, str],
    ) -> list[MatchRow]:
        d = self.cfg.decision
        kept = [
            item
            for position, item in enumerate(ranked)
            if item.explicit or (position < d.store_max and item.score >= d.store_floor)
        ]
        return [
            MatchRow(
                notice_id=notice.notice_id,
                award_key=item.award.award_key,
                piid=item.award.piid,
                method=item.method,
                score=item.score,
                rank=position,
                shown=shown[item.award.award_key],
                evidence=item.evidence(notice),
            )
            for position, item in enumerate(kept, start=1)
            if item.award.award_key not in reviews
        ]
