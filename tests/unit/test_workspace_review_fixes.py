"""Found on the Titanic file: repeated outlier flags, a "0%" that was 0.2%, a note about a column that
models leave out anyway."""

from __future__ import annotations

from sie.workspace import start_session


def data() -> bytes:
    lines = ["id,age,cab,port"]
    for i in range(1000):
        age = "" if i % 5 == 0 else str(20 + i % 15 + (60 if i == 7 else 0))
        cab = "" if i % 10 else f"C{i}"
        port = "" if i < 2 else "SCQ"[i % 3]
        lines.append(f"{i},{age},{cab},{port}")
    return ("\n".join(lines) + "\n").encode()


def test_a_flag_step_is_not_suggested_again_and_a_repeat_is_refused():
    s = start_session(data(), "d.csv")
    step = next(x.step for x in s.suggestions() if x.step.op == "flag_outliers")
    assert s.clean(step).kind == "clean"
    assert not [
        x for x in s.suggestions() if x.step.op == "flag_outliers" and x.step.column == step.column
    ]
    again = s.clean(step)
    assert again.kind == "clean_rejected" and "already flagged" in again.message
    assert len(s.cleaning) == 1


def test_a_share_under_one_percent_is_not_shown_as_zero():
    s = start_session(data(), "d.csv")
    fill = next(
        x for x in s.suggestions() if x.step.op == "fill_missing" and x.step.column == "port"
    )
    assert "0.2%" in fill.reason and " 0% " not in fill.reason


def test_no_many_values_note_for_a_mostly_empty_column():
    big = ["id,cab"] + [f"{i},{f'C{i}' if i % 10 == 0 else ''}" for i in range(1000)]
    s = start_session(("\n".join(big) + "\n").encode(), "d.csv")
    assert not any("different values" in f.text for f in s.workspace.findings)


def test_a_zero_one_target_with_rare_positives_runs_with_class_weights():
    # scikit-learn failed on labels "0"/"1" with balanced class weights (found on a 2% click file)
    from sie.datasets import Limits, LoadOptions, open_dataset, profile
    from sie.workspace import modeling as md

    lines = ["x,z,hit"] + [f"{i % 10},{i % 7},{1 if i % 40 == 0 else 0}" for i in range(800)]
    ds = open_dataset(("\n".join(lines) + "\n").encode(), "r.csv", LoadOptions(), Limits())
    rep, _ = md.run_model(ds, profile(ds), md.ModelSpec("hit"))
    assert "class_weights" in rep.evaluation and rep.verification.status == "verified"


def test_an_id_named_column_that_repeats_across_rows_is_left_out_with_the_warning_reason():
    from sie.datasets import Limits, LoadOptions, open_dataset, profile
    from sie.workspace import modeling as md

    lines = ["patient_id,x,y"] + [f"{i % 60},{i % 9},{i % 4}" for i in range(600)]
    ds = open_dataset(("\n".join(lines) + "\n").encode(), "r.csv", LoadOptions(), Limits())
    rep, _ = md.run_model(ds, profile(ds), md.ModelSpec("y"))
    reason = next(e["reason"] for e in rep.excluded if e["column"] == "patient_id")
    assert "split by id" in reason and "patient_id" not in [f["column"] for f in rep.features]


def test_numbers_with_and_without_thousands_separators_are_offered_for_conversion():
    # "320.12" next to "1,234.50" is typed "mixed" only because of the separators
    lines = ["id,amount"] + [
        f'{i},"{1000 + i:,}.50"' if i % 3 == 0 else f"{i},{i}.25" for i in range(60)
    ]
    s = start_session(("\n".join(lines) + "\n").encode(), "m.csv")
    assert (
        next(c for c in s.workspace.profile.column_profiles if c.name == "amount").type == "mixed"
    )
    step = next(x.step for x in s.suggestions() if x.step.op == "text_to_number")
    assert s.clean(step.model_dump()).kind == "clean"
    assert (
        next(c for c in s.workspace.profile.column_profiles if c.name == "amount").type == "decimal"
    )
