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
