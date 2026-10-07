"""Assemble fetched portal documents into the capture JSON the pipeline already loads. Pure.

The capture is the same shape the owner's browser snippet saves: ``{"medals": {DISC: [rows]},
"standings": {DISC: [records]}}``, plus ``all_standings`` (the official all-country table, used for
reconciliation) and ``at`` when asked for.

``at`` is left out of what the pipeline ingests: it changes on every fetch, so including it would make
every run a "new version" and defeat change detection (docs/DATA_PIPELINE.md section 8). The time of a
fetch is already recorded in ``raw_fetches``. A capture written to a file for reports keeps it.
"""

from __future__ import annotations

import json
from datetime import datetime

from sie.sources.base import RawDocument
from sie.sources.bornan.decode import decode_payload
from sie.sources.bornan.keys import ALL_STANDINGS_KEY


def assemble_capture(documents: dict[str, RawDocument], *, at: datetime | None = None) -> bytes:
    medals: dict[str, object] = {}
    standings: dict[str, object] = {}
    for key, document in documents.items():
        discipline, _, path = key.partition(":")
        if discipline == "ALL":
            continue
        if path == "medals/discipline":
            medals[discipline] = decode_payload(document.content)
        elif path == "medals/standings":
            standings[discipline] = decode_payload(document.content)
    if not medals:
        raise ValueError("no discipline medal documents to assemble")
    if set(medals) != set(standings):
        missing = sorted(set(medals) ^ set(standings))
        raise ValueError(f"medals and standings do not cover the same disciplines: {missing}")
    capture: dict[str, object] = {
        "medals": medals,
        "standings": standings,
        "all_standings": decode_payload(documents[ALL_STANDINGS_KEY].content),
    }
    if at is not None:
        capture["at"] = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
    return json.dumps(capture, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
