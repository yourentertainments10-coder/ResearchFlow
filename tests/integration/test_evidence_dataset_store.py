"""Persisted collected datasets: scoped, hash-addressed, idempotent, atomic, permitted, expiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sie.config import Settings
from sie.datasets import run_operation
from sie.datasets.models import DatasetError
from sie.evidence import (
    EvidenceError,
    NotFound,
    create_project,
    open_scope,
    purge_project,
)
from sie.evidence.collect import collect
from sie.evidence.dataset_store import (
    RowsPermission,
    delete_dataset,
    find_cached,
    list_datasets,
    load_open_licences,
    load_row_policies,
    open_stored,
    prune_expired,
    rows_permission,
    save_dataset,
)
from sie.evidence.errors import DatasetExpired, RowsNotPermitted

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
URL = "https://results.example.gov.in/lok-sabha.html"
WHEN = "2026-10-24T09:00:00+00:00"
PAGE = b"""<table><caption>Seats 2019</caption>
<tr><th>Party</th><th>Seats</th></tr><tr><td>BJP</td><td>303</td></tr><tr><td>INC</td><td>52</td></tr></table>"""
OK = RowsPermission(True, "open_licence: uk-ogl", "uk-ogl", "OGL")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def world(engine, tmp_path):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
        create_project(c, slug="b", name="B", now=NOW)
    with engine.connect() as c:
        a, b = open_scope(c, "a", for_write=True), open_scope(c, "b", for_write=True)
    return {"engine": engine, "root": tmp_path / "store", "a": a, "b": b}


def table(content=PAGE, ctype="text/html", url=URL, **kw):
    return collect(content, ctype, url=url, retrieved_at=WHEN, **kw)


def test_save_then_reopen_returns_the_same_table_and_provenance(world):
    saved = save_dataset(
        world["root"], world["a"], table(), now=NOW, permission=OK, evidence_source_id=7
    )
    assert saved.created and saved.expires_at == NOW + timedelta(days=90)
    got = open_stored(
        world["root"], world["a"], saved.key, now=NOW
    )  # nothing else needed: a later process
    assert got.dataset.columns == ["Party", "Seats"] and got.dataset.frame.loc[1, "Seats"] == "303"
    m = got.manifest
    assert (m["provenance"]["page_url"], m["provenance"]["retrieved_at"]) == (URL, WHEN)
    assert m["provenance"]["table_caption"] == "Seats 2019" and m["provenance"]["table_index"] == 0
    assert (
        m["evidence_source_id"] == 7
        and m["licence"] == "uk-ogl"
        and m["permission_basis"] == "open_licence: uk-ogl"
    )
    assert got.dataset.source.url == URL and got.dataset.source.kind == "fetched"


def test_a_reopened_dataset_answers_questions_without_any_fetch(world):
    saved = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK)
    ds = open_stored(world["root"], world["a"], saved.key, now=NOW).dataset
    r = run_operation(ds, {"op": "describe"})
    assert r.table is not None


def test_only_the_table_and_the_manifest_are_stored_never_the_page(world):
    saved = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK)
    assert sorted(p.name for p in saved.path.iterdir()) == ["manifest.json", "table.json"]
    blob = b"".join(p.read_bytes() for p in saved.path.iterdir())
    assert b"<table>" not in blob and b"<tr>" not in blob


def test_saving_twice_is_idempotent_and_json_types_survive(world):
    js = table(
        b'[{"party":"BJP","seats":303,"share":37.4,"won":true,"note":null}]',
        "application/json",
        url="https://x.gov.in/d",
    )
    a = save_dataset(world["root"], world["a"], js, now=NOW, permission=OK)
    b = save_dataset(world["root"], world["a"], js, now=NOW + timedelta(days=1), permission=OK)
    assert a.key == b.key and a.created and not b.created and a.path == b.path
    row = open_stored(world["root"], world["a"], a.key, now=NOW).dataset.frame.loc[1]
    assert (row["seats"], row["share"], row["won"], row["note"]) == (303, 37.4, True, None)
    assert isinstance(row["seats"], int) and isinstance(row["won"], bool)
    assert len(list_datasets(world["root"], world["a"], now=NOW)) == 1


def test_a_different_table_or_page_gets_its_own_directory(world):
    other = table(PAGE.replace(b"303", b"304"))
    k1 = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    k2 = save_dataset(world["root"], world["a"], other, now=NOW, permission=OK).key
    assert k1 != k2


def test_a_failed_write_publishes_nothing(world, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK)
    monkeypatch.undo()
    base = world["root"] / f"p{world['a'].project_id}"
    assert [p.name for p in base.iterdir() if not p.name.startswith(".tmp-")] == []
    assert list(base.glob(".tmp-*")) == []


def test_projects_are_isolated_and_a_foreign_key_looks_like_a_missing_one(world):
    key = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    with pytest.raises(NotFound) as foreign:
        open_stored(world["root"], world["b"], key, now=NOW)
    with pytest.raises(NotFound) as missing:
        open_stored(world["root"], world["b"], "0" * 12 + "-" + "0" * 8, now=NOW)
    assert str(foreign.value) == str(missing.value)
    assert list_datasets(world["root"], world["b"], now=NOW) == []
    assert find_cached(world["root"], world["b"], page_url=URL, now=NOW) == []
    with pytest.raises(NotFound):
        delete_dataset(world["root"], world["b"], key)
    assert open_stored(world["root"], world["a"], key, now=NOW).key == key  # untouched


def test_a_directory_copied_into_another_project_is_refused(world):
    import shutil

    key = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    dest = world["root"] / f"p{world['b'].project_id}"
    dest.mkdir(parents=True)
    shutil.copytree(world["root"] / f"p{world['a'].project_id}" / key, dest / key)
    with pytest.raises(NotFound):
        open_stored(world["root"], world["b"], key, now=NOW)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "a\\b"])
def test_unsafe_keys_are_not_found(world, bad):
    with pytest.raises(NotFound):
        open_stored(world["root"], world["a"], bad, now=NOW)


def test_nothing_is_stored_without_permission(world):
    with pytest.raises(RowsNotPermitted):
        save_dataset(
            world["root"], world["a"], table(), now=NOW, permission=RowsPermission(False, "none")
        )
    assert not world["root"].exists() or list(world["root"].rglob("table.json")) == []


def test_expired_datasets_are_not_reused_and_can_be_reread_only_on_request(world):
    key = save_dataset(
        world["root"], world["a"], table(), now=NOW, permission=OK, default_retention_days=7
    ).key
    later = NOW + timedelta(days=8)
    with pytest.raises(DatasetExpired):
        open_stored(world["root"], world["a"], key, now=later)
    assert open_stored(world["root"], world["a"], key, now=later, allow_expired=True).key == key
    assert find_cached(world["root"], world["a"], page_url=URL, now=later) == []
    assert list_datasets(world["root"], world["a"], now=later)[0]["state"] == "expired"


def test_saving_again_after_expiry_renews_the_dataset(world):
    first = save_dataset(
        world["root"], world["a"], table(), now=NOW, permission=OK, default_retention_days=7
    )
    later = NOW + timedelta(days=8)
    again = save_dataset(
        world["root"], world["a"], table(), now=later, permission=OK, default_retention_days=7
    )
    assert (
        again.key == first.key and again.created and again.expires_at == later + timedelta(days=7)
    )


def test_a_policy_can_shorten_retention_but_never_extend_it(world):
    short = RowsPermission(True, "host_policy: x", retain_days=3)
    long = RowsPermission(True, "host_policy: x", retain_days=365)
    assert save_dataset(
        world["root"], world["a"], table(), now=NOW, permission=short
    ).expires_at == NOW + timedelta(days=3)
    other = table(PAGE.replace(b"303", b"305"))
    assert save_dataset(
        world["root"], world["a"], other, now=NOW, permission=long, default_retention_days=30
    ).expires_at == NOW + timedelta(days=30)


def test_a_changed_table_file_is_detected(world):
    saved = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK)
    f = saved.path / "table.json"
    data = json.loads(f.read_text())
    data["rows"][0][1] = "999"
    f.chmod(0o644)
    f.write_text(json.dumps(data))
    with pytest.raises(DatasetError, match="no longer matches"):
        open_stored(world["root"], world["a"], saved.key, now=NOW)


def test_find_cached_returns_active_datasets_for_a_page_newest_first(world):
    k1 = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    newer = collect(
        PAGE.replace(b"303", b"306"), "text/html", url=URL, retrieved_at="2026-10-24T09:30:00+00:00"
    )
    k2 = save_dataset(world["root"], world["a"], newer, now=NOW, permission=OK).key
    assert find_cached(world["root"], world["a"], page_url=URL, now=NOW) == [k2, k1]
    assert find_cached(world["root"], world["a"], page_url="https://other.gov.in/x", now=NOW) == []


def test_delete_dataset_removes_it(world):
    key = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    delete_dataset(world["root"], world["a"], key)
    with pytest.raises(NotFound):
        open_stored(world["root"], world["a"], key, now=NOW)


def test_prune_is_a_dry_run_unless_confirmed_and_removes_only_expired(world):
    old = save_dataset(
        world["root"], world["a"], table(), now=NOW - timedelta(days=100), permission=OK
    ).key
    new = save_dataset(world["root"], world["b"], table(), now=NOW, permission=OK).key
    dry = prune_expired(world["root"], now=NOW)
    assert not dry.executed and len(dry.expired) == 1 and old in dry.expired[0]
    assert (world["root"] / f"p{world['a'].project_id}" / old).exists()
    with pytest.raises(EvidenceError):
        prune_expired(world["root"], now=NOW, execute=True)
    done = prune_expired(world["root"], now=NOW, execute=True, confirm="prune")
    assert done.removed == 1 and not (world["root"] / f"p{world['a'].project_id}" / old).exists()
    assert open_stored(world["root"], world["b"], new, now=NOW).key == new


def test_purge_removes_the_projects_dataset_files_and_only_those(world, tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATASET_PATH", str(world["root"]))
    ka = save_dataset(world["root"], world["a"], table(), now=NOW, permission=OK).key
    kb = save_dataset(world["root"], world["b"], table(), now=NOW, permission=OK).key
    dry = purge_project(world["engine"], "a", now=NOW)
    assert any("Would delete 1 stored collected dataset" in line for line in dry.lines)
    assert (world["root"] / f"p{world['a'].project_id}" / ka).exists()
    done = purge_project(
        world["engine"], "a", now=NOW, execute=True, confirm="a", purged_by="Asha Rao"
    )
    assert any("Deleted 1 stored collected dataset" in line for line in done.lines)
    assert not (world["root"] / f"p{world['a'].project_id}").exists()
    assert open_stored(world["root"], world["b"], kb, now=NOW).key == kb


# ---- permission rules -----------------------------------------------------------------------------


def perms(tmp_path, rows=""):
    f = tmp_path / "p.csv"
    f.write_text("host,terms_ref,recorded_on,allows_rows,licence,retain_days,notes\n" + rows)
    return load_row_policies(f)


def test_the_committed_policy_and_licence_files_load():
    assert load_row_policies(ROOT / "data" / "evidence" / "dataset_policies.csv") == {}
    lic = load_open_licences(ROOT / "data" / "evidence" / "open_licences.csv")
    assert "uk-ogl" in lic and "cc-by" in lic and "godl-india" in lic


def test_unknown_sources_are_refused_by_default_and_uploads_are_allowed():
    none = rows_permission(
        kind="web", url="https://x.example.org/a", licence=None, row_policies={}, open_licences={}
    )
    assert not none.allowed
    assert rows_permission(
        kind="upload", url=None, licence=None, row_policies={}, open_licences={}
    ).allowed


def test_an_open_licence_reported_by_the_source_permits_storing_rows(tmp_path):
    lic = load_open_licences(ROOT / "data" / "evidence" / "open_licences.csv")
    ok = rows_permission(
        kind="web",
        url="https://data.gov.uk/x",
        licence="UK-OGL",
        row_policies={},
        open_licences=lic,
    )
    assert ok.allowed and ok.basis == "open_licence: uk-ogl"
    no = rows_permission(
        kind="web",
        url="https://data.gov.uk/x",
        licence="proprietary",
        row_policies={},
        open_licences=lic,
    )
    assert not no.allowed


def test_a_host_policy_permits_or_forbids_and_matches_parent_domains(tmp_path):
    pol = perms(
        tmp_path,
        "eci.gov.in,Terms page 4,2026-10-01,true,GODL,30,\nblocked.org,No reuse,2026-10-01,false,,,\n",
    )
    yes = rows_permission(
        kind="web",
        url="https://results.eci.gov.in/x",
        licence=None,
        row_policies=pol,
        open_licences={},
    )
    assert yes.allowed and yes.retain_days == 30 and yes.terms_ref == "Terms page 4"
    no = rows_permission(
        kind="web",
        url="https://www.blocked.org/x",
        licence="cc-by",
        row_policies=pol,
        open_licences={"cc-by": ""},
    )
    assert not no.allowed  # an explicit refusal beats an open licence label


@pytest.mark.parametrize(
    "rows",
    [
        "x.org,,2026-10-01,true,,,\n",
        "x.org,t,2026-10-01,maybe,,,\n",
        "x.org,t,2026-10-01,true,,0,\n",
        "x.org,t,2026-10-01,true,,,\nx.org,t,2026-10-01,true,,,\n",
        ",t,2026-10-01,true,,,\n",
    ],
)
def test_a_bad_policy_file_is_refused(tmp_path, rows):
    with pytest.raises(EvidenceError):
        perms(tmp_path, rows)


def test_the_dataset_location_and_retention_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("EVIDENCE_DATASET_PATH", str(tmp_path / "x"))
    monkeypatch.setenv("EVIDENCE_DATASET_RETENTION_DAYS", "10")
    s = Settings(_env_file=None, database_url="postgresql://u:p@localhost/x")
    assert s.evidence_dataset_dir == tmp_path / "x" and s.evidence_dataset_retention_days == 10
    monkeypatch.delenv("EVIDENCE_DATASET_PATH")
    monkeypatch.setenv("EVIDENCE_DATASET_RETENTION_DAYS", "0")
    with pytest.raises(Exception):  # noqa: B017
        Settings(_env_file=None, database_url="postgresql://u:p@localhost/x")


def test_the_store_directory_is_git_ignored():
    assert "data/evidence_datasets/" in (ROOT / ".gitignore").read_text()
