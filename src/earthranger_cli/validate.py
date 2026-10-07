"""Validate event_details against an event type's server-rendered schema.

das deliberately does not validate `event_details` on write (see
`schemas/submission.py` there), so a wrong field key, a value outside the
type's choices, a string where a number is expected or a missing required
field is stored silently and only shows up as a broken form later. ER's own
form builder catches all of that in the browser; this module gives the CLI
the same check before anything is sent.

The schema comes from ER already rendered, with choices inlined as enums, so
no choice lookups are needed here:

- v2 types: GET /api/v2.0/activity/eventtypes/<value>/schema?pre_render=true&s_format=enum
  (JSON Schema 2020-12). das strips the strictness keywords from that
  document "for client-side compatibility"; we put `unevaluatedProperties:
  false` back so unknown keys are errors — the same repair `apply` makes.
- v1 types: GET /api/v1.0/activity/events/schema/eventtype/<value>, the
  jinja-rendered legacy envelope (`{"schema": ..., "definition": ...}`); its
  `schema` is draft-04 with enums inline. We add `additionalProperties:
  false` at the top level for the same reason.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import jsonschema
from erclient.er_errors import ERClientNotFound

_V2 = "v2.0"
_UNEXPECTED_RE = re.compile(r"'([^']+)'")


class UnknownEventType(Exception):
    pass


def _fetch_type(client, value: str) -> dict:
    try:
        record = client._get(
            f"activity/eventtypes/{quote(value, safe='')}",
            base_url=client._api_root(_V2),
            max_retries=0,
        )
    except ERClientNotFound:
        raise UnknownEventType(f"no event type with value {value!r}") from None
    if not isinstance(record, dict):
        raise UnknownEventType(f"no event type with value {value!r}")
    return record


def _fetch_json_schema(client, record: dict) -> dict | None:
    """The validatable JSON Schema for a type, tightened against unknown keys,
    or None when the type has no schema (a form-less type accepts anything)."""
    value = record["value"]
    if str(record.get("version", "2")) == "2":
        doc = client._get(
            f"activity/eventtypes/{quote(value, safe='')}/schema",
            base_url=client._api_root(_V2),
            params={"pre_render": "true", "s_format": "enum"},
            max_retries=0,
        )
        schema = doc.get("json") if isinstance(doc, dict) else None
        if not isinstance(schema, dict) or not schema.get("properties"):
            return None
        schema = dict(schema)
        schema.pop("additionalProperties", None)
        schema["unevaluatedProperties"] = False
        return schema
    doc = client._get(f"activity/events/schema/eventtype/{quote(value, safe='')}", max_retries=0)
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, dict) or not schema.get("properties"):
        return None
    schema = dict(schema)
    schema["additionalProperties"] = False
    return schema


def _describe(error: jsonschema.ValidationError, event_type: str) -> list[str]:
    """One message per problem, `field: what is wrong`, in the wording ER's
    own form shows where jsonschema's text is already that."""
    if error.validator in ("unevaluatedProperties", "additionalProperties"):
        keys = _UNEXPECTED_RE.findall(error.message)
        return [f"{k}: not a field of {event_type!r}" for k in keys] or [error.message]
    if error.validator == "required":
        keys = _UNEXPECTED_RE.findall(error.message)
        return [f"{k}: required field is missing" for k in keys[:1]] or [error.message]
    path = ".".join(str(p) for p in error.absolute_path)
    return [f"{path}: {error.message}" if path else error.message]


def validate_events(client, events: list[dict]) -> list[list[str]]:
    """One list of problems per event (empty when it conforms).

    Fetches each event type's schema once per call. Raises UnknownEventType
    for a type the server does not have — that is a batch-level mistake, not
    a per-event validation result.
    """
    validators: dict[str, jsonschema.protocols.Validator | None] = {}
    problems: list[list[str]] = []
    for event in events:
        value = event["event_type"]
        if value not in validators:
            schema = _fetch_json_schema(client, _fetch_type(client, value))
            if schema is None:
                validators[value] = None
            else:
                cls = jsonschema.validators.validator_for(
                    schema, default=jsonschema.Draft202012Validator
                )
                validators[value] = cls(schema)
        validator = validators[value]
        if validator is None:
            problems.append([])
            continue
        details = event.get("event_details") or {}
        messages: list[str] = []
        for err in validator.iter_errors(details):
            messages.extend(_describe(err, value))
        problems.append(sorted(messages))
    return problems
