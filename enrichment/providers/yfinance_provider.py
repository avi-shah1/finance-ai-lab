"""Yahoo Finance via the unofficial `yfinance` library.

Convenient for market data (market cap, exchange, employees) and
standardised statements for any listed company, including non-US ones.
It is NOT a primary source: values are stored as `third_party` /
`medium` confidence and always rank below SEC or Companies House filings.
"""

import math
from datetime import date
from decimal import Decimal

from .. import formatting
from ..base import Fact, NotFound, Provider, ProviderResult, SourceDoc, SourceUnavailable

INCOME_ROWS = {
    "Total Revenue": "revenue",
    "EBITDA": "ebitda",
    "Operating Income": "operating_profit",
    "Pretax Income": "profit_before_tax",
    "Net Income": "net_income",
}
BALANCE_ROWS = {
    "Cash And Cash Equivalents": "cash",
    "Total Debt": "total_debt",
    "Total Assets": "total_assets",
    "Total Liabilities Net Minority Interest": "total_liabilities",
    "Stockholders Equity": "net_assets",
}
ROW_NOTES = {"Total Debt": "Yahoo 'Total Debt' (typically includes lease liabilities)"}
YEARS = 3
UNOFFICIAL = "Unofficial data via yfinance; prefer filed accounts where available."


class YFinance(Provider):
    name = "yfinance"
    label = "Yahoo Finance (yfinance)"

    def not_applicable_reason(self, ctx):
        if ctx.identifiers.get("ticker"):
            return None
        return "Needs a stock ticker. Add one if the company is listed (e.g. CRM, VOD.L)."

    def run(self, ctx):
        try:
            import yfinance as yf
        except ImportError:
            raise SourceUnavailable("yfinance is not installed (pip install -r requirements.txt).")

        symbol = ctx.identifiers["ticker"].upper()
        ticker = yf.Ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception as exc:  # yfinance raises assorted errors for bad symbols / blocks
            raise SourceUnavailable(f"Yahoo Finance request failed: {type(exc).__name__}")
        if not (info.get("longName") or info.get("shortName")):
            raise NotFound(f"Yahoo Finance has no quote for ticker '{symbol}'.")

        today = date.today()
        quote = SourceDoc(provider=self.name, external_id=f"quote:{symbol}", source_type="third_party_database",
                          name="Yahoo Finance", title=f"{info.get('longName') or symbol} quote summary",
                          url=f"https://finance.yahoo.com/quote/{symbol}", published_on=today, notes=UNOFFICIAL)

        def add(src, fact_type, value_text, **kw):
            src.facts.append(Fact(fact_type=fact_type, value_text=value_text, evidence_type="third_party",
                                  confidence="medium", **kw))

        add(quote, "stock_ticker", symbol, location="Quote summary")
        if info.get("fullExchangeName"):
            add(quote, "exchange", info["fullExchangeName"], location="Quote summary")
        if _num(info.get("marketCap")) is not None:
            cap = _num(info["marketCap"])
            add(quote, "market_cap", formatting.money(cap, info.get("currency")), value_number=cap,
                currency=info.get("currency"), as_of=today, period=f"as at {formatting.day(today)}",
                location="Quote summary: market cap")
        if _num(info.get("fullTimeEmployees")) is not None:
            n = _num(info["fullTimeEmployees"])
            add(quote, "employee_count", f"{formatting.count(n)} (full-time)", value_number=n, as_of=today,
                location="Quote summary: full-time employees")
        if info.get("longBusinessSummary"):
            add(quote, "description", formatting.squash(info["longBusinessSummary"], 600),
                location="Quote summary: business summary")

        statements = SourceDoc(provider=self.name, external_id=f"financials:{symbol}",
                               source_type="third_party_database", name="Yahoo Finance",
                               title=f"{symbol} annual financial statements",
                               url=f"https://finance.yahoo.com/quote/{symbol}/financials",
                               published_on=today, notes=UNOFFICIAL)
        currency = info.get("financialCurrency") or info.get("currency")
        problems = []
        for frame_name, rows in (("income_stmt", INCOME_ROWS), ("balance_sheet", BALANCE_ROWS)):
            try:
                frame = getattr(ticker, frame_name)
            except Exception as exc:
                problems.append(f"{frame_name}: {type(exc).__name__}")
                continue
            if frame is None or frame.empty:
                continue
            flow = frame_name == "income_stmt"
            for row, fact_type in rows.items():
                if row not in frame.index:
                    continue
                series = frame.loc[row]
                for col in sorted(series.index, reverse=True)[:YEARS]:
                    value = _num(series[col])
                    if value is None:
                        continue
                    end = col.date()
                    add(statements, fact_type, formatting.money(value, currency), value_number=value,
                        currency=currency, period_end=end,
                        period=f"FY ended {formatting.day(end)}" if flow else f"as at {formatting.day(end)}",
                        location=f"Yahoo {frame_name.replace('_', ' ')}: {row}",
                        notes=ROW_NOTES.get(row, UNOFFICIAL))

        result = ProviderResult(sources=[quote, statements], website=info.get("website"))
        if problems:
            result.status = "partial"
            result.message = "Some statements unavailable: " + "; ".join(problems)
        else:
            n = len(quote.facts) + len(statements.facts)
            result.message = f"{symbol}: {n} third-party facts (unofficial)."
        return result


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return Decimal(str(round(f)))
