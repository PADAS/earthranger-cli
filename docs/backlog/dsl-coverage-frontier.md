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

Not refused, silently flattened: **sub-choices** (a choice nested under a
parent choice, `Choice.sub_choice_of` in das). Pull emits the child as a
plain option and apply never touches parent links, because the choices
REST API neither returns nor accepts them (2026-10-20); the Django admin is
the only way to set one. Blocked on ERA-14343 (expose `sub_choice_of` in
the API); the CLI follow-up is GUNDI-5828 (parent reference as
`<choice set>:<value>` in the spec, spec authoritative).

Related but separate: `docs/backlog/json-file-import.md` (parked feature),
and two post-merge hardening notes from the first whole-branch review —
consider a sturdier check than the django-filter message-string match in
`client.get_choices`, and setting choice `ordernum` on options is done but
event-type dropdown ordering across *unmanaged* types remains the server's.
