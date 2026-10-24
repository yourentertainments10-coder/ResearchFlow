"""Collected datasets: stored once, deduplicated, integrity-checked, reusable for follow-ups."""

from __future__ import annotations

import json
import os
import stat

import pytest

from research_fakes import NOW, RESULTS_CSV
from sie.datasets.provenance import sha256_bytes
from sie.evidence.errors import EvidenceError
from sie.evidence.web import FetchedPage
from sie.research.collect import collect_page
from sie.research.store import ResearchStore, dataset_id

URL = "https://data.example.gov.uk/election/results.csv"


def table(body=RESULTS_CSV, ctype="text/csv", url=URL):
    page = FetchedPage(url, url, 200, ctype, body.encode(), NOW, NOW)
    return collect_page(page)[0]


def test_saving_twice_stores_one_copy_and_reports_it(tmp_path):
    store = ResearchStore(tmp_path)
    first = store.save(table(), title="Election results")
    second = store.save(table())
    assert not first.already_present and second.already_present and first.id == second.id
    assert len(store.list_datasets()) == 1 and store.meta(first.id)["title"] == "Election results"
    assert [p.name for p in (tmp_path / "datasets").iterdir()] == [first.id]


def test_files_are_read_only_and_no_temp_directory_remains(tmp_path):
    saved = ResearchStore(tmp_path).save(table())
    for f in saved.path.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o400
    assert not [p for p in (tmp_path / "datasets").iterdir() if p.name.startswith(".tmp-")]


def test_load_round_trips_rows_hash_and_provenance(tmp_path):
    store = ResearchStore(tmp_path)
    t = table()
    saved = store.save(t)
    back = store.load(saved.id)
    assert back.dataset.source.sha256 == t.dataset.source.sha256
    assert back.rows == 4 and back.url == URL and back.retrieved_at == NOW.isoformat()
    assert back.raw_sha256 == sha256_bytes(RESULTS_CSV.encode())
    assert back.provenance_frame().equals(t.provenance_frame())


def test_a_different_page_or_table_gets_a_different_id():
    other = table("party,seats\nA,1\n", url="https://data.example.gov.uk/other.csv")
    assert dataset_id(table()) != dataset_id(other)


def test_a_changed_stored_file_is_refused_on_load(tmp_path):
    store = ResearchStore(tmp_path)
    saved = store.save(table())
    f = saved.path / "source.csv"
    os.chmod(saved.path, 0o700)
    os.chmod(f, 0o600)
    f.write_text("party,seats\nTampered,1\n")
    with pytest.raises(EvidenceError, match="no longer matches"):
        store.load(saved.id)


def test_unknown_and_malformed_ids_are_refused(tmp_path):
    store = ResearchStore(tmp_path)
    with pytest.raises(EvidenceError):
        store.meta("0123456789abcdef")
    with pytest.raises(EvidenceError):
        store.meta("../etc/passwd")


def test_json_tables_reload_from_their_derived_bytes(tmp_path):
    body = json.dumps({"result": {"records": [{"party": "A", "seats": 3}]}})
    store = ResearchStore(tmp_path)
    saved = store.save(table(body, "application/json", "https://x.example.gov.uk/d"))
    back = store.load(saved.id)
    assert back.rows == 1 and any(f.startswith("unwrapped_json") for f in back.flags)
    assert back.raw_sha256 == sha256_bytes(body.encode())


def test_reuse_matches_questions_by_title_address_and_columns(tmp_path):
    store = ResearchStore(tmp_path)
    store.save(table(), title="General election results by party")
    (meta, share), *rest = store.reuse("how many seats did each party win in the general election")
    assert meta["url"] == URL and share >= 0.5 and rest == []
    assert store.reuse("rainfall in kerala") == []
    assert store.reuse("the of and") == []  # nothing searchable


def test_list_on_an_empty_store_is_empty(tmp_path):
    assert ResearchStore(tmp_path / "none").list_datasets() == []
