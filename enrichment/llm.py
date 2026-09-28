"""The ONLY place in the app that calls the Anthropic API.

The model is used purely as an extractor or assessor: it reads material we
already retrieved and returns JSON that must match a Pydantic schema.
Everything it returns is then checked by the calling code before anything
is stored.

Each call names a *task*, and the task decides the model: cheap Haiku for
routine reading and classification, Sonnet for buyer-fit assessments, where
judgement matters. Every call's tokens and estimated cost are recorded in
the api_usage table (see usage.py).

Configuration (in .env):
    ANTHROPIC_API_KEY             required
    ANTHROPIC_MODEL_<TASK>        optional per-task override,
                                  e.g. ANTHROPIC_MODEL_DEEP_DOCUMENTS=claude-sonnet-5
"""

import os

from .base import CredentialsMissing, EnrichmentError, RateLimited, SourceUnavailable

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"

TASK_MODELS = {
    # Discovery and screening (every search)
    "plan_queries": HAIKU,       # turn buyer/sector/geography into search queries
    "triage": HAIKU,             # is each discovered company in scope?
    "buyer_profile": HAIKU,      # facts about the buyer from its own pages
    "screening_extract": HAIKU,  # facts about a candidate from pages and news
    "assessment": SONNET,        # buyer-fit ratings with cited evidence
    # Deep research (only when requested)
    "deep_documents": HAIKU,     # figures from filed PDF accounts
    "deep_website": HAIKU,       # facts from the company's website pages
}

# If a model's safety filter declines a request, the API can re-run it on a
# fallback model inside the same call. Only some models accept this option.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")


def model_for(task):
    if task not in TASK_MODELS:
        raise ValueError(f"Unknown LLM task {task!r}; add it to TASK_MODELS.")
    return os.environ.get(f"ANTHROPIC_MODEL_{task.upper()}") or TASK_MODELS[task]


def require_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CredentialsMissing("API credentials required: add ANTHROPIC_API_KEY to .env.")


def extract(system, content, schema, task, max_tokens=16000, search_id=None, company_id=None):
    """Send `content` (a list of content blocks) using the model for `task`
    and return (validated schema instance, metadata dict). The call's cost is
    recorded against `search_id` / `company_id` when given. Raises typed
    enrichment errors instead of SDK errors so callers can record a clear status."""
    require_key()
    import anthropic
    import pydantic

    import usage

    model = model_for(task)
    options = {}
    if model.startswith(FALLBACK_MODELS):
        options = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    client = anthropic.Anthropic(max_retries=3)
    try:
        response = client.beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            **options,
        )
    except anthropic.AuthenticationError:
        raise CredentialsMissing("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.PermissionDeniedError as exc:
        raise CredentialsMissing(f"Anthropic API permission denied: {exc.message}")
    except anthropic.RateLimitError:
        raise RateLimited("Anthropic API rate limit reached. Try again in a minute.")
    except anthropic.BadRequestError as exc:
        raise EnrichmentError(f"Anthropic API rejected the request: {exc.message}")
    except anthropic.APIStatusError as exc:
        raise SourceUnavailable(f"Anthropic API error (HTTP {exc.status_code}). Try again later.")
    except anthropic.APIConnectionError:
        raise SourceUnavailable("Could not reach the Anthropic API.")
    except pydantic.ValidationError:
        raise EnrichmentError("The model's answer did not match the required schema; nothing was stored.")

    # Tokens are billed even when the answer turns out to be unusable, so
    # record the cost before checking the result.
    u = response.usage
    meta = {
        "model": response.model,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0,
        "request_id": response._request_id,
    }
    meta["cost_usd"] = usage.claude_cost(meta["model"], meta["input_tokens"], meta["output_tokens"],
                                         meta["cache_read_tokens"], meta["cache_write_tokens"])
    usage.record_claude(task, meta, search_id=search_id, company_id=company_id)

    if response.stop_reason == "refusal":
        raise EnrichmentError("The model declined to process this material; nothing was stored.")
    if response.stop_reason == "max_tokens":
        raise EnrichmentError("The model's answer was cut off before it finished; nothing was stored.")
    if response.parsed_output is None:
        raise EnrichmentError("The model returned no structured answer; nothing was stored.")
    return response.parsed_output, meta
