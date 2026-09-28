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


def _criterion_model(dimension):
    return create_model(
        f"{dimension['code'].title().replace('_', '')}Criterion",
        criterion=(Literal[tuple(dimension["criteria"])], ...),
        rating=(Rating, ...),
        evidence_ids=(list[str], EVIDENCE_IDS),
        reason=(str, Field(description="At most 25 words, referring to the cited evidence.")),
    )


def _dimension_model(dimension):
    return create_model(
        f"{dimension['code'].title().replace('_', '')}Assessment",
        rating=(Rating, ...),
        facts=(str, Field(description=(
            "What the cited evidence shows, restated plainly with no judgement. 1-3 sentences."))),
        interpretation=(str, Field(description=(
            "Your judgement of what that means for this buyer, and the main uncertainty. 1-3 sentences."))),
        evidence_ids=(list[str], EVIDENCE_IDS),
        criteria=(list[_criterion_model(dimension)], Field(description="One entry per criterion of this dimension.")),
    )


class OverallAssessment(BaseModel):
    rating: OverallRating
    rationale: str = Field(description="2-3 sentences drawing on the four dimensions, including the biggest risk.")
    evidence_ids: list[str] = EVIDENCE_IDS


AssessmentResult = create_model(
    "AssessmentResult",
    **{d["code"]: (_dimension_model(d), ...) for d in screening.DIMENSIONS},
    overall=(OverallAssessment, ...),
)
