"""refresh.yml must still send the health alert when the refresh step fails.

GitHub skips the steps after a failed one unless they say otherwise. The health check and the
alert-state steps therefore need an ``if`` that is true after a failure; ``!cancelled()`` also lets a
manual cancel stop them.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/refresh.yml"


def steps() -> dict[str, str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^      - ", text.split("    steps:\n", 1)[1])
    out = {}
    for block in blocks[1:]:
        name = re.search(r"name: (.+)", block)
        out[name.group(1).strip() if name else block.splitlines()[0]] = block
    return out


def test_health_and_alert_state_steps_run_after_a_failed_refresh():
    found = steps()
    for name in ("Restore alert state", "Health check and alerts", "Save alert state"):
        assert re.search(r"if: (\$\{\{ !cancelled\(\) \}\}|always\(\))", found[name]), name


def test_the_refresh_step_itself_is_not_made_to_continue_on_error():
    block = steps()["Refresh with locking and retries"]
    assert "continue-on-error" not in block and "if:" not in block  # a failure must stay visible
