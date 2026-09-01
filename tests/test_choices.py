from earthranger_cli.choices import ChoiceOp, desired_choice_sets, plan_field_choices
from earthranger_cli.dsl import EventTypeSpec, FieldSpec, OptionSpec


def _et():
    return EventTypeSpec(
        value="t1",
        display="T1",
        fields=[
            FieldSpec(
                key="species",
                label="Species",
                type="select",
                options=[OptionSpec("elephant", "Elephant"), OptionSpec("lion", "Lion")],
            ),
            FieldSpec(key="notes", label="Notes", type="string"),
        ],
    )


def _spec_of(et):
    from earthranger_cli.dsl import CategorySpec, Spec

    return Spec(category=CategorySpec(value="c", display="C"), event_types=[et])


def test_desired_choice_sets_only_for_choice_fields():
    records = desired_choice_sets(_spec_of(_et()))
    assert list(records) == ["t1_species"]
    assert records["t1_species"][0] == {
        "model": "activity.event",
        "field": "t1_species",
        "value": "elephant",
        "display": "Elephant",
        "is_active": True,
        "ordernum": 0,
    }


def test_desired_choice_sets_truncate_to_100():
    et = EventTypeSpec(
        value="t",
        display="T",
        fields=[
            FieldSpec(key="f", label="F", type="select", options=[OptionSpec("v" * 150, "d" * 150)])
        ],
    )
    rec = desired_choice_sets(_spec_of(et))["t_f"][0]
    assert len(rec["value"]) == 100
    assert len(rec["display"]) == 100


DESIRED = [
    {
        "model": "activity.event",
        "field": "t1_species",
        "value": "elephant",
        "display": "Elephant",
        "is_active": True,
    },
    {
        "model": "activity.event",
        "field": "t1_species",
        "value": "lion",
        "display": "Lion",
        "is_active": True,
    },
]


def test_plan_create_when_missing():
    ops = plan_field_choices([], DESIRED)
    assert [op.action for op in ops] == ["create", "create"]
    assert ops[0].payload == DESIRED[0]


def test_plan_unchanged_when_identical():
    existing = [
        {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": True},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert [op.action for op in ops] == ["unchanged", "unchanged"]


def test_plan_update_on_display_change_and_reactivation():
    existing = [
        {"id": "1", "value": "elephant", "display": "Old Name", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": False},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert ops[0] == ChoiceOp(
        action="update",
        value="elephant",
        payload={"display": "Elephant", "is_active": True},
        choice_id="1",
    )
    assert ops[1].action == "update"
    assert ops[1].payload == {"display": "Lion", "is_active": True}


def test_plan_deactivates_options_removed_from_spec():
    existing = [
        {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": True},
        {"id": "3", "value": "rhino", "display": "Rhino", "is_active": True},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert ops[-1] == ChoiceOp(
        action="deactivate", value="rhino", payload={"is_active": False}, choice_id="3"
    )


def test_plan_leaves_already_inactive_removed_options_alone():
    existing = [{"id": "3", "value": "rhino", "display": "Rhino", "is_active": False}]
    ops = plan_field_choices(existing, DESIRED)
    assert [op.action for op in ops] == ["create", "create"]


def test_desired_records_use_explicit_choices_field():
    et = _et()
    et.fields[0].choices_field = "shared_species"
    records = desired_choice_sets(_spec_of(et))
    assert list(records) == ["shared_species"]
    assert records["shared_species"][0]["field"] == "shared_species"


def test_desired_choice_sets_merges_toplevel_and_inline():
    from earthranger_cli.dsl import parse_spec

    spec = parse_spec(
        {
            "category": {"value": "c1", "display": "C1"},
            "choices": {"shared": [{"value": "a", "display": "A", "icon": "ico"}]},
            "event_types": [
                {
                    "value": "t1",
                    "display": "T1",
                    "fields": [
                        {"key": "x", "label": "X", "type": "select", "choices_field": "shared"},
                        {"key": "y", "label": "Y", "type": "select", "options": ["b", "c"]},
                    ],
                },
            ],
        }
    )
    sets = desired_choice_sets(spec)
    assert sorted(sets) == ["shared", "t1_y"]
    assert sets["shared"] == [
        {
            "model": "activity.event",
            "field": "shared",
            "value": "a",
            "display": "A",
            "is_active": True,
            "ordernum": 0,
            "icon": "ico",
        },
    ]
    assert [r["ordernum"] for r in sets["t1_y"]] == [0, 1]
    assert all("icon" not in r for r in sets["t1_y"])


def test_plan_updates_on_ordernum_and_icon_changes():
    desired = [
        {
            "model": "activity.event",
            "field": "f",
            "value": "a",
            "display": "A",
            "is_active": True,
            "ordernum": 0,
            "icon": "new_icon",
        },
        {
            "model": "activity.event",
            "field": "f",
            "value": "b",
            "display": "B",
            "is_active": True,
            "ordernum": 1,
        },
    ]
    existing = [
        {
            "id": "1",
            "value": "a",
            "display": "A",
            "is_active": True,
            "ordernum": 5,
            "icon": "old_icon",
        },
        {"id": "2", "value": "b", "display": "B", "is_active": True, "ordernum": 1, "icon": None},
    ]
    ops = plan_field_choices(existing, desired)
    assert ops[0].action == "update"
    assert ops[0].payload == {"display": "A", "is_active": True, "ordernum": 0, "icon": "new_icon"}
    assert ops[1].action == "unchanged"


def test_plan_null_ordernum_converges():
    desired = [
        {
            "model": "activity.event",
            "field": "f",
            "value": "a",
            "display": "A",
            "is_active": True,
            "ordernum": 0,
        },
    ]
    existing = [{"id": "1", "value": "a", "display": "A", "is_active": True}]
    ops = plan_field_choices(existing, desired)
    assert ops[0].action == "update"
    assert ops[0].payload["ordernum"] == 0
