"""Build the static dashboard (one self-contained HTML file, no server, free to host anywhere).

Charts are plain SVG/HTML drawn in the page, so nothing is loaded from the network. Colour follows
the dataviz method: one-hue ordinal ramp for medal type, validated categorical slots for countries
and gender, one-hue sequential ramp for the heatmap. Every chart has a tooltip and the tables below
carry every number.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

CAPTURED_ISO = "2026-10-04T16:21:31Z"  # time of the owner capture (capture file field "at")
TEMPLATE = (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")


def _event_count(df: pd.DataFrame) -> int:
    """Distinct events. A portal event code is only unique within its discipline."""
    if "event_id" in df.columns:
        return int(df["event_id"].nunique())
    return df.groupby(["discipline", "event_code"]).ngroups


def build(placings_csv: Path, captured_iso: str, out: Path, events: int) -> Path:
    df = pd.read_csv(placings_csv).fillna({"date": ""})
    # Until the committed reports are regenerated, placings.csv may still carry the portal event code.
    event_col = "event_id" if "event_id" in df.columns else "event_code"
    data = [
        {
            "c": r.country_code, "cn": r.country_name, "s": r.discipline_name, "e": getattr(r, event_col),
            "en": r.event_name, "g": r.gender, "m": r.medal, "d": r.date,
        }
        for r in df.itertuples()
    ]  # fmt: skip
    local = datetime.fromisoformat(captured_iso.replace("Z", "+00:00")).astimezone(
        ZoneInfo("Asia/Kolkata")
    )
    label = f"{local.day} {local:%b %Y} · {local:%H:%M} IST"
    meta = {"label": label, "events": events, "nodate": int((df["date"] == "").sum())}
    html = (
        TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__META__", json.dumps(meta))
        .replace("__CAPTURED_LABEL__", label)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    df = pd.read_csv(root / "reports/placings.csv")
    events = _event_count(df)
    site = root / "site"
    path = build(root / "reports/placings.csv", CAPTURED_ISO, site / "index.html", events)
    shutil.copy(
        root / "reports/asian_games_2026_full_analysis.xlsx",
        site / "asian_games_2026_full_analysis.xlsx",
    )
    print(path, path.stat().st_size)


if __name__ == "__main__":
    main()
