"""Turn a pytest JUnit report into GitHub Actions error annotations.

Annotations appear in the check run UI and through the checks API, which is useful where the raw
job log is not reachable. Standard library only. Usage: ``python annotate_failures.py report.xml``.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_ANNOTATIONS = 10  # GitHub shows at most 10 error annotations per step
MAX_MESSAGE_CHARS = 3000


def _escape(value: str, *, is_property: bool = False) -> str:
    """Escape text for a workflow command (https://docs.github.com/actions/reference/workflow-commands)."""
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if is_property:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def annotations(report: Path) -> list[str]:
    """Return one ``::error`` command per failed or errored test, at most MAX_ANNOTATIONS."""
    lines: list[str] = []
    for case in ET.parse(report).iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            message = f"{node.get('message') or ''}\n{node.text or ''}".strip()[-MAX_MESSAGE_CHARS:]
            title = _escape(f"pytest {kind}: {name[:200]}", is_property=True)
            lines.append(f"::error title={title}::{_escape(message)}")
            if len(lines) >= MAX_ANNOTATIONS:
                return lines
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: annotate_failures.py REPORT.xml", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"no report at {report}; nothing to annotate")
        return 0
    for line in annotations(report):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
