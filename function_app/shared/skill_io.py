"""
Azure AI Search Custom WebApi Skill request/response envelope.

Each route receives:
{
  "values": [
    { "recordId": "0", "data": { ... inputs ... } },
    ...
  ]
}

And must return:
{
  "values": [
    { "recordId": "0", "data": { ... outputs ... }, "errors": [], "warnings": [] },
    ...
  ]
}

CRITICAL contract: a single record cannot return BOTH `data` and
`errors` populated at the same time. Azure Search rejects such
responses with: "Web Api response contains both data and errors.
Will not process." When a record fails, return `data: null` (not
an empty dict — empty dict still trips the constraint) and put the
failure in `errors`. The indexer logs the error in its execution
status; the chunk doesn't land in the index, which is the right
outcome — a partially-processed chunk would mislead retrieval.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import azure.functions as func

from .config import ConfigError


def handle_skill_request(
    req: func.HttpRequest,
    record_processor: Callable[[dict[str, Any]], dict[str, Any]],
) -> func.HttpResponse:
    try:
        body = req.get_json()
    except (ValueError, Exception) as exc:  # noqa: B014 -- defensive: any parse failure
        # Catch ANY parse failure (not just ValueError). Some Azure
        # Functions versions raise different exception types for
        # malformed/missing bodies, and an uncaught exception here
        # surfaces in the indexer log as "Web Api response status:
        # 'InternalServerError'" with no useful detail.
        logging.warning("skill request body parse failed: %s", exc)
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not isinstance(body, dict):
        return func.HttpResponse("Body must be a JSON object", status_code=400)
    values_in = body.get("values") or []
    if not isinstance(values_in, list):
        return func.HttpResponse(
            "Body 'values' must be a list", status_code=400,
        )
    values_out = []

    for record in values_in:
        record_id = record.get("recordId", "0")
        data_in = record.get("data", {}) or {}
        try:
            data_out = record_processor(data_in) or {}
            values_out.append({
                "recordId": record_id,
                "data": data_out,
                "errors": [],
                "warnings": [],
            })
        except ConfigError as exc:
            # Misconfigured Function App: surface as a clean per-record
            # error so the indexer's execution status shows exactly what
            # is missing, instead of a 500 from this whole skill batch.
            # `data: null` is required by Azure's "either data or errors,
            # never both" contract — see module docstring.
            logging.error("record %s config error: %s", record_id, exc)
            values_out.append({
                "recordId": record_id,
                "data": None,
                "errors": [{"message": f"ConfigError: {exc}"}],
                "warnings": [],
            })
        except Exception as exc:
            logging.exception("record %s failed: %s", record_id, exc)
            values_out.append({
                "recordId": record_id,
                "data": None,
                "errors": [{"message": f"{type(exc).__name__}: {exc}"}],
                "warnings": [],
            })

    return func.HttpResponse(
        json.dumps({"values": values_out}),
        mimetype="application/json",
        status_code=200,
    )
