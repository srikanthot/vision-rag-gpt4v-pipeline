"""
Azure OpenAI client wrapper.

Prefers managed-identity auth (AUTH_MODE=mi, the default). Falls back to
API key auth when AUTH_MODE=key is set or AOAI_API_KEY is provided and
MI is disabled.

The `openai` package is imported lazily inside get_client() rather than
at module top-level so that:
  - shared modules that transitively `from .aoai import ...` (e.g. when
    a test pulls `normalize_figure_ref` out of diagram.py) don't fail
    at import time on a machine without `openai` installed
  - the package is still required at runtime for any code path that
    actually instantiates the client (preanalyze vision calls, the
    Function App vision skill)

Both requirements files declare `openai`; the lazy import is for
defensive dev-time UX, not a license to skip the dependency.
"""

from functools import lru_cache

from .config import optional_env, required_env
from .credentials import (
    AOAI_SCOPE,
    bearer_token_provider,
    use_managed_identity,
)


@lru_cache(maxsize=1)
def get_client():
    """Returns an AzureOpenAI client. Imported lazily so that loading
    this module does not require the `openai` package — only calling
    this function does."""
    from openai import AzureOpenAI
    # 2024-12-01-preview is the minimum API version that supports gpt-4.1
    # chat + vision deployments. Override via AOAI_API_VERSION app setting.
    api_version = optional_env("AOAI_API_VERSION", "2024-12-01-preview")
    endpoint = required_env("AOAI_ENDPOINT")

    # 60-second hard timeout on AOAI calls. Default in the openai SDK is
    # 600s (10 min); under load that lets a hanging chat/vision request
    # consume the entire function-host worker timeout and cascade-kill
    # other in-flight calls. 60s is well above p99 for our prompts
    # (vision ~3-15s, summary ~5-20s).
    #
    # CRITICAL: max_retries=0. The openai SDK default is 2 retries with
    # exponential backoff PLUS honoring Retry-After (which AOAI sets to
    # 60s on 429). That means a single throttled call can hang for up to
    # 60s call + 60s wait + 60s call + 60s wait + 60s call = ~5 min
    # WALL CLOCK before either succeeding or failing. Multiplied across
    # 200 figures per PDF = 16+ hours of vision retries on a quota-
    # throttled period, blowing every 230s Azure WebApi skill timeout
    # and pinning function-app workers for the entire indexer cycle.
    # The Azure AI Search indexer ALREADY retries failed records itself
    # (maxFailedItemsPerBatch is the real budget). Fail fast here and
    # let the indexer's higher-level retry coordinate -- it spreads the
    # work over a longer window without locking up a function worker.
    if use_managed_identity():
        return AzureOpenAI(
            api_version=api_version,
            azure_endpoint=endpoint,
            azure_ad_token_provider=bearer_token_provider(AOAI_SCOPE),
            timeout=60.0,
            max_retries=0,
        )

    return AzureOpenAI(
        api_key=required_env("AOAI_API_KEY"),
        api_version=api_version,
        azure_endpoint=endpoint,
        timeout=60.0,
        max_retries=0,
    )


def vision_deployment() -> str:
    return required_env("AOAI_VISION_DEPLOYMENT")


def chat_deployment() -> str:
    return required_env("AOAI_CHAT_DEPLOYMENT")
