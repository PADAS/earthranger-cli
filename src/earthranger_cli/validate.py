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
  document "for client-side compatibility" — on the root and on every
  collection item, including collections inside conditional sections; we put
  `unevaluatedProperties: false` back in exactly those places (mirroring
  das's `remove_strict_validation_properties`) so unknown keys are errors.
  Fixed-shape objects (location, attachment items) and `if` predicates are
  left alone, as das leaves them.
- v1 types: GET /api/v1.0/activity/events/schema/eventtype/<value>, the
  jinja-rendered legacy envelope (`{"schema": ..., "definition": ...}`); its
  `schema` is draft-04 with enums inline. We add `additionalProperties:
  false` in the same places. The v2 event-type detail endpoint serves v2
  types only, so a 404 there is followed by this endpoint before a type is
  declared unknown.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import jsonschema
from erclient.er_errors import ERClientNotFound

from .read import get_json

_V2 = "v2.0"
_UNEXPECTED_RE = re.compile(r"'([^']+)'")


class UnknownEventType(Exception):
    pass


_ATTACHMENT_ITEM_KEYS = {"uploadId"}


def _collection_fields(parent: dict):
    """`parent`'s collection fields: arrays whose items are user-defined objects.
    Attachment arrays (items shaped exactly {uploadId}) are fixed-shape and
    excluded — the same rule das applies when it relaxes a schema."""
    properties = parent.get("properties")
    if not isinstance(properties, dict):
        return
    for field in properties.values():
        items = field.get("items") if isinstance(field, dict) else None
        if (
            not isinstance(items, dict)
            or field.get("type") != "array"
            or items.get("type") != "object"
        ):
            continue
        item_properties = items.get("properties", {})
        if not isinstance(item_properties, dict) or set(item_properties) == _ATTACHMENT_ITEM_KEYS:
            continue
        yield field


def _tighten(node: dict, keyword: str) -> None:
    """Restore `keyword: false` on one data-bearing object and its nested
    collection items (in place). The inverse of das's `_relax_data_object`."""
    node.pop("additionalProperties", None)
    node.pop("unevaluatedProperties", None)
    node[keyword] = False
    for field in _collection_fields(node):
        _tighten(field["items"], keyword)


def _tightened(schema: dict, keyword: str) -> dict:
    """A deep copy of `schema` with strictness restored where das strips it:
    the root, its collection items, and collection items declared under
    conditional sections (`allOf[*].then`). `if` predicates are untouched."""
    import copy

    schema = copy.deepcopy(schema)
    _tighten(schema, keyword)
    for entry in schema.get("allOf") or []:
        if isinstance(entry, dict) and isinstance(entry.get("then"), dict):
            for field in _collection_fields(entry["then"]):
                _tighten(field["items"], keyword)
    return schema


def _v1_schema(client, value: str) -> dict | None:
    """The v1 rendered schema, or None when the type has no schema.
    Raises UnknownEventType on 404 — this is the last place a type can be."""
    try:
        doc = get_json(client, f"activity/events/schema/eventtype/{quote(value, safe='')}")
    except ERClientNotFound:
        raise UnknownEventType(f"no event type with value {value!r}") from None
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, dict) or not schema.get("properties"):
        return None
    return _tightened(schema, "additionalProperties")


def _fetch_json_schema(client, value: str) -> dict | None:
    """The validatable JSON Schema for a type, tightened against unknown keys,
    or None when the type has no schema (a form-less type accepts anything).

    The v2 detail endpoint lists v2 types only, so a 404 there means "v1 or
    unknown" and the v1 schema endpoint decides which.
    """
    path = f"activity/eventtypes/{quote(value, safe='')}"
    try:
        record = get_json(client, path, base_url=client._api_root(_V2))
    except ERClientNotFound:
        return _v1_schema(client, value)
    if not isinstance(record, dict) or str(record.get("version", "2")) != "2":
        return _v1_schema(client, value)
    doc = get_json(
        client,
        f"{path}/schema",
        base_url=client._api_root(_V2),
        params={"pre_render": "true", "s_format": "enum"},
    )
    schema = doc.get("json") if isinstance(doc, dict) else None
    if not isinstance(schema, dict) or not schema.get("properties"):
        return None
    return _tightened(schema, "unevaluatedProperties")


def _describe(error: jsonschema.ValidationError, event_type: str) -> list[str]:
    """One message per problem, `path.to.field: what is wrong`, in the wording
    ER's own form shows where jsonschema's text is already that. The path
    is kept for every kind of error so a problem inside a collection item
    (`animals.2.count`) says which item."""
    where = ".".join(str(p) for p in error.absolute_path)
    prefix = f"{where}." if where else ""
    if error.validator in ("unevaluatedProperties", "additionalProperties"):
        keys = _UNEXPECTED_RE.findall(error.message)
        return [f"{prefix}{k}: not a field of {event_type!r}" for k in keys] or [error.message]
    if error.validator == "required":
        keys = _UNEXPECTED_RE.findall(error.message)
        return [f"{prefix}{k}: required field is missing" for k in keys[:1]] or [error.message]
    return [f"{where}: {error.message}" if where else error.message]


def validate_events(client, events: list[dict]) -> list[list[str]]:
    """One list of problems per event (empty when it conforms).

    Fetches each event type's schema once per call. Raises UnknownEventType
    for a type the server does not have — that is a batch-level mistake, not
    a per-event validation result.
    """
    validators: dict[str, jsonschema.protocols.Validator | None] = {}
    problems: list[list[str]] = []
    for event in events:
        value = event.get("event_type")
        if not isinstance(value, str) or not value:
            raise UnknownEventType(f"event_type must be a non-empty string, got {value!r}")
        if value not in validators:
            schema = _fetch_json_schema(client, value)
            if schema is None:
                validators[value] = None
            else:
                cls = jsonschema.validators.validator_for(
                    schema, default=jsonschema.Draft202012Validator
                )
                validators[value] = cls(schema)
        validator = validators[value]
        # Only omission means "no details". An explicit null or any other
        # non-object would be sent unchanged and refused by das, so it is a
        # problem regardless of what the schema says (the CLI's batch loader
        # already refuses these; this guards programmatic callers).
        details = event.get("event_details", {})
        if not isinstance(details, dict):
            problems.append([f"event_details must be an object of field values, got {details!r}"])
            continue
        if validator is None:
            problems.append([])
            continue
        errors = list(validator.iter_errors(details))
        # When a conditional section's `then` branch fails deeper down (a bad
        # item inside its collection), 2020-12 no longer counts that branch's
        # fields as evaluated, so the root also reports the section's field as
        # unknown. The deeper error is the real one; drop the echo.
        known_roots = {str(e.absolute_path[0]) for e in errors if len(e.absolute_path)}
        messages: list[str] = []
        for err in errors:
            if err.validator in ("unevaluatedProperties", "additionalProperties") and not len(
                err.absolute_path
            ):
                keys = [k for k in _UNEXPECTED_RE.findall(err.message) if k not in known_roots]
                messages.extend(f"{k}: not a field of {value!r}" for k in keys)
                continue
            messages.extend(_describe(err, value))
        problems.append(sorted(messages))
    return problems
