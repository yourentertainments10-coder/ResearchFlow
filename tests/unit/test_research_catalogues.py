"""Free catalogue adapters and the registry (ADR-045). Simulated network only: no real request is made."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from research_fakes import (
    CKAN,
    ELECTION_DATASET,
    NOW,
    OPENALEX,
    Site,
    ckan_result,
    json_page,
    web_fetcher,
)
from sie.evidence.discovery import discover, load_authority
from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.catalogues import (
    CatalogueEntry,
    CkanCatalogue,
    OpenAlexCatalogue,
    PaidAccessRefused,
    RegisteredHostFetcher,
    build_providers,
    load_catalogues,
)
from sie.research.guard import SearchProviderAdapter

ROOT = Path(__file__).resolve().parents[2]
HEADER = "name,kind,base_url,access,verified_on,terms_ref,quota_note\n"
ENTRY = "x,ckan,https://data.example.org/api/action,{access},2026-10-24,https://example.org/terms,none\n"


def cached(tmp_path, site):
    return CachedFetcher(
        web_fetcher(site),
        FetchCache(tmp_path / "c"),
        max_age=__import__("datetime").timedelta(hours=24),
        now=lambda: NOW,
    )


def entries():
    return load_catalogues(ROOT / "data" / "evidence" / "catalogues.csv")


def test_the_shipped_registry_lists_only_free_keyless_catalogues_with_a_terms_link():
    loaded = entries()
    assert {e.name for e in loaded} == {"data.gov.uk", "openalex"}
    for e in loaded:
        assert e.access == "free_no_key" and e.verified_on and e.terms_ref.startswith("https://")


@pytest.mark.parametrize("access", ["paid", "free_with_key", "key", "free", "", "paid_fallback"])
def test_any_access_other_than_free_keyless_is_refused_at_load(access, tmp_path):
    path = tmp_path / "c.csv"
    path.write_text(HEADER + ENTRY.format(access=access))
    with pytest.raises(PaidAccessRefused):
        load_catalogues(path)


@pytest.mark.parametrize(
    "row",
    [
        "x,ckan,https://data.example.org/api?api_key=abc,free_no_key,2026-10-24,https://t.org/,n\n",
        "x,ckan,http://data.example.org/api,free_no_key,2026-10-24,https://t.org/,n\n",
        "x,ckan,https://data.example.org/api,free_no_key,,https://t.org/,n\n",
        "x,ckan,https://data.example.org/api,free_no_key,2026-10-24,,n\n",
        "x,solr,https://data.example.org/api,free_no_key,2026-10-24,https://t.org/,n\n",
        "x,ckan,https://127.0.0.1/api,free_no_key,2026-10-24,https://t.org/,n\n",
    ],
)
def test_unsafe_or_incomplete_registry_rows_are_refused(row, tmp_path):
    path = tmp_path / "c.csv"
    path.write_text(HEADER + row)
    with pytest.raises(EvidenceError):
        load_catalogues(path)


def test_duplicate_names_and_wrong_columns_are_refused(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text(HEADER + ENTRY.format(access="free_no_key") * 2)
    with pytest.raises(EvidenceError, match="unique"):
        load_catalogues(path)
    path.write_text("name,kind\nx,ckan\n")
    with pytest.raises(EvidenceError, match="columns"):
        load_catalogues(path)
    assert load_catalogues(tmp_path / "missing.csv") == ()


def ckan(tmp_path, site):
    entry = next(e for e in entries() if e.kind == "ckan")
    return CkanCatalogue(entry, cached(tmp_path, site)), site


def test_ckan_gives_one_lead_per_csv_or_json_resource_with_licence(tmp_path):
    site = Site({CKAN: json_page(ckan_result(ELECTION_DATASET))})
    provider, _ = ckan(tmp_path, site)
    hits = provider.search("general election results", 10)
    assert [h.url for h in hits] == ["https://data.example.gov.uk/election/results.csv"]
    assert hits[0].licence == "uk-ogl" and hits[0].source_name == "data.gov.uk"
    assert hits[0].title == "General election results by party - Results"
    api = site.api_requests()[0]
    q = parse_qs(urlsplit(api).query)
    assert api.startswith(CKAN) and q["q"] == ["general election results"] and q["rows"] == ["10"]


def test_a_dataset_without_a_data_file_becomes_a_dataset_page_lead(tmp_path):
    ds = {**ELECTION_DATASET, "resources": [{"url": "https://x.org/a.pdf", "format": "PDF"}]}
    provider, _ = ckan(tmp_path, Site({CKAN: json_page(ckan_result(ds))}))
    (hit,) = provider.search("election", 5)
    assert hit.url == "https://data.gov.uk/dataset/general-election-results"


def test_ckan_rows_are_capped_and_the_query_is_quoted(tmp_path):
    site = Site({CKAN: json_page(ckan_result())})
    provider, _ = ckan(tmp_path, site)
    provider.search("a&b=c d", 500)
    api = site.api_requests()[0]
    assert "rows=100" in api and "a%26b%3Dc%20d" in api


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": {"message": "x"}},
        {"success": True},
        {"success": True, "result": {"results": "nope"}},
        {"success": True, "result": {"results": [{"notitle": 1}]}},
        [],
    ],
)
def test_ckan_fails_loudly_on_an_unexpected_shape(payload, tmp_path):
    provider, _ = ckan(tmp_path, Site({CKAN: json_page(payload)}))
    with pytest.raises(EvidenceError):
        provider.search("q", 5)


def test_ckan_refuses_a_non_json_response(tmp_path):
    page = RawResponse(200, {"content-type": "text/html"}, b"<html>captcha</html>")
    provider, _ = ckan(tmp_path, Site({CKAN: page}))
    with pytest.raises(EvidenceError, match="expected JSON"):
        provider.search("q", 5)


def test_openalex_leads_are_keyless_and_carry_no_key_or_account_parameter(tmp_path):
    work = {
        "title": "Voting in India",
        "doi": "https://doi.org/10.1/x",
        "publication_date": "2024-01-01",
    }
    site = Site({OPENALEX: json_page({"results": [work, {"no": "title"}]})})
    entry = next(e for e in entries() if e.kind == "openalex")
    provider = OpenAlexCatalogue(entry, cached(tmp_path, site))
    (hit,) = provider.search("voting", 5)
    assert hit.url == "https://doi.org/10.1/x" and hit.published == "2024-01-01"
    api = site.api_requests()[0]
    assert api.startswith(OPENALEX) and not any(
        k in api.lower() for k in ("key", "mailto", "token")
    )


def test_openalex_fails_loudly_without_a_results_list(tmp_path):
    entry = next(e for e in entries() if e.kind == "openalex")
    provider = OpenAlexCatalogue(entry, cached(tmp_path, Site({OPENALEX: json_page({"x": 1})})))
    with pytest.raises(EvidenceError):
        provider.search("q", 5)


def test_an_adapter_only_accepts_its_own_kind(tmp_path):
    e = entries()
    with pytest.raises(EvidenceError):
        CkanCatalogue(next(x for x in e if x.kind == "openalex"), None)
    with pytest.raises(EvidenceError):
        OpenAlexCatalogue(next(x for x in e if x.kind == "ckan"), None)


def test_provider_calls_can_only_reach_registered_hosts(tmp_path):
    site = Site()
    guarded = RegisteredHostFetcher(cached(tmp_path, site), entries())
    with pytest.raises(FetchRefused) as err:
        guarded.fetch("https://paid-search.example.com/v1/search?q=x")
    assert err.value.code == "unregistered_host" and site.requests == []


def test_discovery_over_free_catalogues_ranks_the_official_data_file_first(tmp_path):
    site = Site(
        {
            CKAN: json_page(ckan_result(ELECTION_DATASET)),
            OPENALEX: json_page(
                {
                    "results": [
                        {"title": "General election results study", "doi": "https://doi.org/10.1/y"}
                    ]
                }
            ),
        }
    )
    providers = build_providers(entries(), cached(tmp_path, site))
    adapter = SearchProviderAdapter(providers)
    rules = load_authority(ROOT / "data" / "evidence" / "source_authority.csv")
    result = discover(adapter, "general election results", rules=rules, limit=5)
    assert [c.url for c in result.candidates][
        0
    ] == "https://data.example.gov.uk/election/results.csv"
    top = result.candidates[0]
    assert top.tier == 1 and top.structured and top.licence == "uk-ogl"
    assert {o.status for o in adapter.outcomes} == {"ok"}


def test_catalogue_entry_exposes_its_host():
    e = CatalogueEntry(
        "n", "ckan", "https://data.gov.uk/api/action", "free_no_key", "d", "https://t", "q"
    )
    assert e.host == "data.gov.uk"


def test_the_data_gov_uk_request_is_the_documented_package_search_get(tmp_path):
    """Pinned to the examples in https://guidance.data.gov.uk/get_data/api_documentation/ (read
    2026-10-24): base ``https://data.gov.uk/api/action/``, ``package_search`` with ``q`` and ``rows``,
    as a GET. The page also says "post a JSON dictionary"; every example on it is a GET, and CKAN serves
    both. The safe fetcher only does GET, so this is checked in the controlled live test."""
    site = Site({CKAN: json_page(ckan_result())})
    CkanCatalogue(entries()[0], cached(tmp_path, site)).search("general election", 7)
    assert site.api_requests() == [
        "https://data.gov.uk/api/action/package_search?q=general%20election&rows=7"
    ]


def test_redirect_hosts_are_optional_and_must_be_plain_host_names(tmp_path):
    head = "name,kind,base_url,access,verified_on,terms_ref,quota_note"
    row = "x,ckan,https://data.example.gov.uk/api/action,free_no_key,2026-10-24,https://t.example/terms,none"
    old = tmp_path / "old.csv"
    old.write_text(f"{head}\n{row}\n")
    assert load_catalogues(old)[0].redirect_host_list == ()
    good = tmp_path / "good.csv"
    good.write_text(f"{head},redirect_hosts\n{row},ckan.example.gov.uk;www.example.gov.uk\n")
    assert load_catalogues(good)[0].redirect_host_list == (
        "ckan.example.gov.uk",
        "www.example.gov.uk",
    )
    for bad in ("10.0.0.5", "https://x.example.gov.uk", "localhost", "a.example.gov.uk/path"):
        f = tmp_path / "bad.csv"
        f.write_text(f"{head},redirect_hosts\n{row},{bad}\n")
        with pytest.raises(EvidenceError):
            load_catalogues(f)


def test_the_committed_data_gov_uk_entry_names_the_host_its_api_redirects_to():
    entry = {e.name: e for e in load_catalogues(ROOT / "data" / "evidence" / "catalogues.csv")}
    assert entry["data.gov.uk"].redirect_host_list == ("ckan.publishing.service.gov.uk",)
