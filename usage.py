"""What each paid API call cost, recorded in the api_usage table.

Costs are estimates at public list prices (checked 2026-09-27), so the app
can show what a search or a deep-research run cost. They are not a bill:
check the Anthropic, Exa and Brave dashboards for actual charges.

Recording never interrupts the work it describes: if the database write
fails, the problem is logged and the caller carries on.
"""

import logging
from decimal import Decimal

log = logging.getLogger(__name__)

# USD per million tokens: (input, output)
CLAUDE_PRICES = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5":  (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5":    (Decimal("5.00"), Decimal("25.00")),
}
CACHE_WRITE_MULTIPLIER = Decimal("1.25")   # writing a prompt-cache entry
CACHE_READ_MULTIPLIER = Decimal("0.10")    # reading one back

# USD per billable unit
SERVICE_PRICES = {
    ("exa", "search"): Decimal("0.007"),     # $7 per 1,000 searches
    ("exa", "page"): Decimal("0.001"),       # $1 per 1,000 pages of text
    ("brave", "search"): Decimal("0.005"),   # $5 per 1,000 searches
}
MILLION = Decimal(1_000_000)


def _claude_price(model):
    """Prices for a model id. The API may answer with a longer, dated id
    (e.g. claude-haiku-4-5-20251001), so match on the known prefix."""
    for known in sorted(CLAUDE_PRICES, key=len, reverse=True):
        if model and model.startswith(known):
            return CLAUDE_PRICES[known]
    return None


def claude_cost(model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
    """Estimated USD cost of one Claude call, or None for an unknown model."""
    price = _claude_price(model)
    if price is None:
        return None
    input_price, output_price = price
    total = (input_tokens * input_price
             + cache_write_tokens * input_price * CACHE_WRITE_MULTIPLIER
             + cache_read_tokens * input_price * CACHE_READ_MULTIPLIER
             + output_tokens * output_price)
    return total / MILLION


def record_claude(task, meta, search_id=None, company_id=None):
    """Record one Claude call. `meta` is the dict returned by llm.extract."""
    _record(service="anthropic", task=task, model=meta.get("model"),
            input_tokens=meta.get("input_tokens", 0), output_tokens=meta.get("output_tokens", 0),
            cache_read_tokens=meta.get("cache_read_tokens", 0),
            cache_write_tokens=meta.get("cache_write_tokens", 0),
            units=0, cost_usd=meta.get("cost_usd"), search_id=search_id, company_id=company_id,
            request_id=meta.get("request_id"))


def record_service(service, task, unit, count, search_id=None, company_id=None, cost_usd=None):
    """Record `count` billable units (e.g. 1 Exa search, 5 pages of text).
    Pass `cost_usd` when the service reports what the call actually cost;
    otherwise it is estimated from SERVICE_PRICES."""
    if cost_usd is None:
        price = SERVICE_PRICES.get((service, unit))
        cost_usd = price * count if price is not None else None
    _record(service=service, task=task, model=None, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0, units=count,
            cost_usd=cost_usd,
            search_id=search_id, company_id=company_id, request_id=None)


def _record(**row):
    try:
        import database
        database.insert_api_usage(row)
    except Exception:
        log.exception("Could not record API usage for task %s", row.get("task"))
