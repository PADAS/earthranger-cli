# DSL coverage frontier

Constructs `er events pull` refuses by name (2026-09-01). Each is deliberate
scope, not a bug: support one when a real site produces it (the refusal
message will say which event type). Every stock category tested so far
(`security`, `monitoring`) round-trips with zero warnings.

- **Condition operators beyond `is_exactly`** — CONTAINS, IS_EMPTY,
  IS_NOT_EMPTY, IS_CONTAINED_BY, IS_NOT_CONTAINED_BY (each has its own
  gnarly `if` encoding in das `eventtype_meta_schemas.py`); also multiple
  conditions per section, and typed (non-string) condition values.
- **Choice-list and nested-collection sub-fields inside collections.**
- **Location fields** (`LOCATION` UI type) and **headers**.
- **Non-positional section ids**, `auto-generate` schemas, per-field
  `pattern` validation.
- **Category `ordernum`** (event-type ordernum is supported; categories are
  a two-line addition when wanted).

Related but separate: `docs/backlog/json-file-import.md` (parked feature),
and two post-merge hardening notes from the first whole-branch review —
consider a sturdier check than the django-filter message-string match in
`client.get_choices`, and setting choice `ordernum` on options is done but
event-type dropdown ordering across *unmanaged* types remains the server's.
