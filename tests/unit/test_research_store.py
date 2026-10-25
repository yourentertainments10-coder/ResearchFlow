"""Research store: a thin door onto the shared dataset store. Permission, expiry, project label, reload."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import pytest

from research_fakes import NOW, RESULTS_CSV
from sie.evidence.errors import DatasetExpired, EvidenceError
from sie.evidence.web import FetchedPage
from sie.research.collect import collect_page
from sie.research.store import ResearchStore, StoreRules

ROOT = Path(__file__).resolve().parents[2]
URL = "https://data.example.gov.uk/election/results.csv"
RULES = StoreRules.load(ROOT / "data" / "evidence")


def table(body=RESULTS_CSV, ctype="text/csv", url=URL, licence="uk-ogl"):
    page = FetchedPage(url, url, 200, ctype, body.encode(), NOW, NOW)
    return dataclasses.replace(collect_page(page)[0], licence=licence, catalogue="data.gov.uk")


def store(tmp_path, project="default", now=NOW):
    return ResearchStore(tmp_path, project=project, rules=RULES, now=lambda: now)


def test_saving_twice_keeps_one_copy(tmp_path):
    s = store(tmp_path)
    first, second = s.save(table(), title="Election"), s.save(table())
    assert first.stored and not first.already_present and second.already_present
    assert first.id == second.id and len(s.list_datasets()) == 1


def test_only_the_normalised_table_and_manifest_are_kept_never_the_page(tmp_path):
    s = store(tmp_path)
    saved = s.save(table())
    files = sorted(p.name for p in (tmp_path / "r-default" / saved.id).iterdir())
    assert files == ["manifest.json", "table.json"]
    assert "<html" not in (tmp_path / "r-default" / saved.id / "table.json").read_text()


def test_reload_round_trips_rows_provenance_and_licence(tmp_path):
    s = store(tmp_path)
    saved = s.save(table())
    back = s.load(saved.id)
    assert back.rows == 4 and back.url == URL and back.retrieved_at == NOW.isoformat()
    assert back.licence == "uk-ogl" and back.catalogue == "data.gov.uk" and back.stored_id
    assert back.label == "csv"


def test_rows_are_not_saved_without_a_licence_or_policy_that_permits_it(tmp_path):
    s = store(tmp_path)
    for lic in (None, "all-rights-reserved"):
        r = s.save(table(licence=lic))
        assert not r.stored and "no recorded permission" in r.basis
    assert s.list_datasets() == []


def test_a_dataset_expires_and_is_neither_listed_nor_loadable_after_that(tmp_path):
    saved = store(tmp_path).save(table())
    later = store(tmp_path, now=NOW + timedelta(days=91))
    assert later.list_datasets() == [] and len(later.list_datasets(include_expired=True)) == 1
    with pytest.raises(DatasetExpired):
        later.load(saved.id)
    assert later.prune().expired and later.list_datasets(include_expired=True)  # dry run keeps it
    later.prune(execute=True)
    assert later.list_datasets(include_expired=True) == []


def test_projects_are_separate_and_labels_are_validated(tmp_path):
    a, b = store(tmp_path, "a"), store(tmp_path, "b")
    saved = a.save(table())
    assert b.list_datasets() == []
    with pytest.raises(EvidenceError):
        b.load(saved.id)
    with pytest.raises(EvidenceError):
        store(tmp_path, "../x")


def test_a_changed_stored_table_is_refused(tmp_path):
    s = store(tmp_path)
    saved = s.save(table())
    f = tmp_path / "r-default" / saved.id / "table.json"
    f.write_text(f.read_text().replace("Alpha", "Tampered"))
    with pytest.raises(Exception, match="no longer matches"):
        s.load(saved.id)


def test_json_tables_reload_with_their_flags(tmp_path):
    body = json.dumps({"result": {"records": [{"party": "A", "seats": 3}]}})
    s = store(tmp_path)
    back = s.load(s.save(table(body, "application/json", "https://x.example.gov.uk/d")).id)
    assert back.rows == 1 and any(f.startswith("unwrapped_json") for f in back.flags)
