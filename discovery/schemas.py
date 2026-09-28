"""The JSON shapes Claude must return during discovery (enforced by
structured outputs). Field descriptions are part of the instructions."""

from typing import Literal

from pydantic import BaseModel, Field

from enrichment import formatting


class QueryPlan(BaseModel):
    queries: list[str] = Field(description=(
        "6 to 8 search queries. Each describes a TYPE of company in natural language, as it "
        "might describe itself on its homepage, including the geography, e.g. "
        "'German software company providing patient scheduling for hospitals'. "
        "Never include the buyer's name or any specific company name."))


class TriageItem(BaseModel):
    candidate_id: str = Field(description="The id given for the candidate, e.g. C3.")
    company_name: str = Field(description="The company's name as the page presents it.")
    is_operating_company: bool = Field(description=(
        "True if the page is a company's own site. False for directories, news sites, "
        "marketplaces, job boards, investor or consultancy listings."))
    sector_fit: Literal["yes", "partial", "no", "unclear"]
    geography_fit: Literal["yes", "no", "unclear"] = Field(description=(
        "'unclear' when the page doesn't say where the company is based or operates."))
    relevance: Literal["high", "medium", "low", "none"] = Field(description=(
        "How plausible a target this is for the search, judged only from the page text."))
    summary: list[str] = Field(description=formatting.SUMMARY_BULLET_RULES)
    match_reason: str = Field(description="One sentence: why it does or doesn't fit the search, citing what the text says.")


class TriageResult(BaseModel):
    items: list[TriageItem]
