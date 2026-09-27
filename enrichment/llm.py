"""The ONLY place in the app that calls the Anthropic API.

The model is used purely as an extractor: it reads material we already
retrieved and returns JSON that must match a Pydantic schema. Everything it
returns is then checked against the source text by the calling provider
before anything is stored.

Configuration (in .env):
    ANTHROPIC_API_KEY             required
    ANTHROPIC_EXTRACTION_MODEL    optional, default claude-opus-5
"""

import os

from .base import CredentialsMissing, EnrichmentError, RateLimited, SourceUnavailable

DEFAULT_MODEL = "claude-opus-5"

# If the model's safety filter declines a request, the API re-runs it on a
# fallback model inside the same call. Each fact records the model that
# actually answered (response.model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model_name():
    return os.environ.get("ANTHROPIC_EXTRACTION_MODEL") or DEFAULT_MODEL


def require_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CredentialsMissing("API credentials required: add ANTHROPIC_API_KEY to .env.")


def extract(system, content, schema, max_tokens=16000):
    """Send `content` (a list of content blocks) and return
    (validated schema instance, metadata dict). Raises typed enrichment
    errors instead of SDK errors so the runner can record a clear status."""
    require_key()
    import anthropic
    import pydantic

    client = anthropic.Anthropic(max_retries=3)
    try:
        response = client.beta.messages.parse(
            model=model_name(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            betas=[FALLBACK_BETA],
            fallbacks="default",
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

    if response.stop_reason == "refusal":
        raise EnrichmentError("The model declined to process this material; nothing was stored.")
    if response.stop_reason == "max_tokens":
        raise EnrichmentError("The model's answer was cut off before it finished; nothing was stored.")
    if response.parsed_output is None:
        raise EnrichmentError("The model returned no structured answer; nothing was stored.")

    meta = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "request_id": response._request_id,
    }
    return response.parsed_output, meta
