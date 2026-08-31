from er_events_cli.choices import ChoiceOp, desired_choice_records, plan_field_choices
from er_events_cli.dsl import EventTypeSpec, FieldSpec, OptionSpec


def _et():
    return EventTypeSpec(
        value="t1", display="T1",
        fields=[
            FieldSpec(key="species", label="Species", type="select",
                      options=[OptionSpec("elephant", "Elephant"), OptionSpec("lion", "Lion")]),
            FieldSpec(key="notes", label="Notes", type="string"),
        ],
    )


def test_desired_choice_records_only_for_choice_fields():
    records = desired_choice_records(_et())
    assert list(records) == ["t1_species"]
    assert records["t1_species"][0] == {
        "model": "activity.event",
        "field": "t1_species",
        "value": "elephant",
        "display": "Elephant",
        "is_active": True,
    }


def test_desired_choice_records_truncate_to_100():
    et = EventTypeSpec(
        value="t", display="T",
        fields=[FieldSpec(key="f", label="F", type="select",
                          options=[OptionSpec("v" * 150, "d" * 150)])],
    )
    rec = desired_choice_records(et)["t_f"][0]
    assert len(rec["value"]) == 100
    assert len(rec["display"]) == 100


DESIRED = [
    {"model": "activity.event", "field": "t1_species", "value": "elephant",
     "display": "Elephant", "is_active": True},
    {"model": "activity.event", "field": "t1_species", "value": "lion",
     "display": "Lion", "is_active": True},
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
    assert ops[0] == ChoiceOp(action="update", value="elephant",
                              payload={"display": "Elephant", "is_active": True}, choice_id="1")
    assert ops[1].action == "update"
    assert ops[1].payload == {"display": "Lion", "is_active": True}


def test_plan_deactivates_options_removed_from_spec():
    existing = [
        {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": True},
        {"id": "3", "value": "rhino", "display": "Rhino", "is_active": True},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert ops[-1] == ChoiceOp(action="deactivate", value="rhino",
                               payload={"is_active": False}, choice_id="3")


def test_plan_leaves_already_inactive_removed_options_alone():
    existing = [{"id": "3", "value": "rhino", "display": "Rhino", "is_active": False}]
    ops = plan_field_choices(existing, DESIRED)
    assert [op.action for op in ops] == ["create", "create"]
