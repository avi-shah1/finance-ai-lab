"""The JSON shapes the model must return (enforced by structured outputs).

Every target figure has its own slot that must be either a Figure or null,
so "not disclosed" is an explicit answer rather than a silent omission.
The field descriptions are part of the instructions the model sees.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


class Figure(BaseModel):
    value: float = Field(description=(
        "The number exactly as printed for the CURRENT reporting period, before applying "
        "unit_multiplier. Negative if printed in brackets or described as a loss."))
    unit_multiplier: Literal[1, 1000, 1000000, 1000000000] = Field(description=(
        "1 if printed in whole units; 1000 if the column is headed e.g. £'000 or £000; "
        "1000000 for £m."))
    currency: Optional[str] = Field(description="ISO 4217 code such as GBP or USD; null for counts like employees.")
    page: int = Field(description="1-based page number, within the PDF you were given, where the figure is printed.")
    evidence_text: str = Field(description=(
        "Text copied character-for-character from that page containing the label and the figure, "
        "e.g. the table row 'Turnover 3 12,400 11,020'."))
    confidence: Confidence


class TextValue(BaseModel):
    value: str
    page: int = Field(description="1-based page number within the PDF you were given.")
    evidence_text: str = Field(description="Text copied character-for-character from that page.")
    confidence: Confidence


class AccountsExtraction(BaseModel):
    company_name_on_document: Optional[str]
    period_start: Optional[str] = Field(description="Start of the current reporting period, YYYY-MM-DD, if stated.")
    period_end: Optional[str] = Field(description="End of the current reporting period / balance sheet date, YYYY-MM-DD.")
    revenue: Optional[Figure] = Field(description="Turnover or revenue for the current period. null if not disclosed.")
    operating_profit: Optional[Figure] = Field(description="Operating profit/(loss). null if not disclosed.")
    profit_before_tax: Optional[Figure] = Field(description="Profit/(loss) before taxation. null if not disclosed.")
    net_income: Optional[Figure] = Field(description="Profit/(loss) for the financial year after tax. null if not disclosed.")
    cash: Optional[Figure] = Field(description="Cash at bank and in hand, or cash and cash equivalents. null if not disclosed.")
    total_debt: Optional[Figure] = Field(description=(
        "Total bank loans and borrowings, ONLY if a single printed total exists. Do not add up items."))
    creditors_due_within_one_year: Optional[Figure] = Field(description="Creditors: amounts falling due within one year.")
    creditors_due_after_one_year: Optional[Figure] = Field(description="Creditors: amounts falling due after more than one year.")
    total_assets_less_current_liabilities: Optional[Figure]
    net_assets: Optional[Figure] = Field(description="Net assets/(liabilities), or total equity / shareholders' funds.")
    employee_count: Optional[Figure] = Field(description="Average number of employees in the period. unit_multiplier 1, currency null.")
    auditor: Optional[TextValue] = Field(description="Name of the audit firm, if the accounts are audited.")
    notes: Optional[str] = Field(description=(
        "Anything an analyst should know, e.g. 'micro-entity accounts: no profit and loss account filed'."))


WebsiteFactType = Literal["product", "capability", "industry_served", "customer_segment",
                          "notable_customer", "integration", "geography_presence"]


class WebsiteItem(BaseModel):
    fact_type: WebsiteFactType
    value: str = Field(description="Short name, e.g. 'Patient Scheduling' or 'NHS trusts'. At most a few words.")
    source_url: str = Field(description="The URL of the page (exactly as given) that states this.")
    evidence_text: str = Field(description="Text copied character-for-character from that page that states it.")
    confidence: Confidence


class WebsiteExtraction(BaseModel):
    items: list[WebsiteItem]
