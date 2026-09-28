"""The JSON shapes Claude must return during screening (enforced by
structured outputs). Field descriptions are part of the instructions."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, create_model

import screening

# ---------------------------------------------------------------------------
# News -> dated events
# ---------------------------------------------------------------------------

EventType = Literal[tuple(screening.EVENT_TYPES)]


class NewsEvent(BaseModel):
    article_id: str = Field(description="The id of the article that reports it, e.g. A2.")
    about_this_company: bool = Field(description=(
        "True only if the article is clearly about the company named in the request "
        "(not a different company with a similar name)."))
    event_type: EventType
    title: str = Field(description="What happened, in at most 12 words.")
    event_date: Optional[str] = Field(description="YYYY-MM-DD or YYYY-MM if the article states when; otherwise null.")
    summary: str = Field(description="One sentence with the key details (amounts, parties, places) as stated.")
    evidence_text: str = Field(description="Text copied character-for-character from the article that reports it.")


class NewsExtraction(BaseModel):
    events: list[NewsEvent]


# ---------------------------------------------------------------------------
# Buyer-fit assessment
# ---------------------------------------------------------------------------

Rating = Literal["very_strong", "strong", "mixed", "weak", "insufficient_evidence"]
OverallRating = Literal[tuple(screening.OVERALL_VIEWS)]
EVIDENCE_IDS = Field(description="IDs of the evidence lines relied on, e.g. ['F120', 'E7']. Empty if none.")

# One shared criterion type and one shared dimension type keep the schema
# small: the API compiles the schema into a grammar with a size limit, and
# four near-identical per-dimension types exceeded it. That each criterion
# belongs to its dimension is checked in code (assess.to_rows).


class Criterion(BaseModel):
    criterion: str = Field(description="The criterion's code exactly as listed for this dimension, e.g. product_adjacency.")
    rating: Rating
    evidence_ids: list[str] = EVIDENCE_IDS
    reason: str = Field(description="At most 25 words, referring to the cited evidence.")


class Dimension(BaseModel):
    rating: Rating
    gist: str = Field(description=(
        "The deciding point in at most 12 words, for a summary box, e.g. "
        "'Dental software is far from the buyer's CRM core'. Plain prose, no evidence IDs."))
    facts: str = Field(description="What the cited evidence shows, restated plainly with no judgement. 1-3 sentences.")
    interpretation: str = Field(description=(
        "Your judgement of what that means for this buyer, and the main uncertainty. 1-3 sentences."))
    evidence_ids: list[str] = EVIDENCE_IDS
    criteria: list[Criterion] = Field(description="One entry per criterion listed for this dimension.")


class Point(BaseModel):
    text: str = Field(description="One short sentence, at most 20 words.")
    evidence_ids: list[str] = EVIDENCE_IDS


class OverallAssessment(BaseModel):
    rating: OverallRating
    headline: str = Field(description=(
        "The verdict in one sentence of at most 30 words, leading with the deciding reason, "
        "e.g. 'Already majority-owned by Henry Schein One, so not independently available.'"))
    rationale: str = Field(description="2-3 sentences drawing on the four dimensions, including the biggest risk.")
    evidence_ids: list[str] = EVIDENCE_IDS
    strengths: list[Point] = Field(description=(
        "Up to 3 substantive reasons this could be a good acquisition (fit, customers, growth), each citing evidence. "
        "Being small or cheap is not a strength."))
    risks: list[Point] = Field(description=(
        "Up to 3 substantive risks or blockers, most important strategic concern first. Cite evidence where it "
        "exists; a risk can also be a gap in the evidence. A size mismatch is only a risk if it blocks a deal."))
    next_checks: list[str] = Field(description=(
        "Up to 4 short questions an analyst should answer next. One must ask who owns the target and whether "
        "they would be open to a sale."))


AssessmentResult = create_model(
    "AssessmentResult",
    **{d["code"]: (Dimension, ...) for d in screening.DIMENSIONS},
    overall=(OverallAssessment, ...),
)
