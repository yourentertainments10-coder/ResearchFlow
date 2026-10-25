"""Source discovery and ranking (ADR-044): deterministic rules, stated reasons, robots pre-check."""

from __future__ import annotations

from pathlib import Path

import pytest

from sie.evidence import EvidenceError
from sie.evidence.discovery import (
    SearchHit,
    authority_for,
    discover,
    load_authority,
    terms,
)
from sie.evidence.web import RawResponse, WebFetcher

ROOT = Path(__file__).resolve().parents[2]
RULES = load_authority(ROOT / "data" / "evidence" / "source_authority.csv")
Q = "How many Lok Sabha seats did BJP win in 2019 election results"


class Fake:
    def __init__(self, hits):
        self.hits = hits
        self.asked = []

    def search(self, query, limit):
        self.asked.append((query, limit))
        return self.hits


HITS = [
    SearchHit("https://example-blog.com/bjp-wins", "BJP election wins explained", "Lok Sabha seats 2019"),
    SearchHit("https://en.wikipedia.org/wiki/2019_Indian_general_election", "2019 Indian general election", "Lok Sabha seats BJP won"),
    SearchHit("https://results.eci.gov.in/files/2019/lok-sabha-results.csv", "Lok Sabha election results 2019", "seats won by party BJP"),
    SearchHit("http://insecure.example.org/results", "Lok Sabha results", "seats"),
    SearchHit("https://results.eci.gov.in/files/2019/lok-sabha-results.csv", "dup", "dup"),
    SearchHit("https://cooking.example.org/pasta", "Pasta recipe", "dinner"),
    SearchHit("https://localhost/admin", "Lok Sabha", "x"),
]  # fmt: skip


def test_the_committed_authority_file_loads_and_classifies():
    assert authority_for(RULES, "results.eci.gov.in") == (1, "government")
    assert authority_for(RULES, "eci.nic.in") == (1, "government")
    assert authority_for(RULES, "en.wikipedia.org") == (3, "reference")
    assert authority_for(RULES, "example-blog.com") == (4, "unclassified")
    assert authority_for(RULES, "notgov.in") == (4, "unclassified")  # a suffix must align on a dot
    assert authority_for(RULES, "RESULTS.ECI.GOV.IN.") == (1, "government")


def test_longest_pattern_wins(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("pattern,tier,category,note\n.gov.in,1,government,\n.shady.gov.in,4,override,\n")
    rules = load_authority(f)
    assert authority_for(rules, "x.shady.gov.in") == (4, "override")
    assert authority_for(rules, "x.gov.in") == (1, "government")


@pytest.mark.parametrize(
    "body",
    [
        "pattern,tier\n.gov,1\n",
        "pattern,tier,category,note\ngov,1,x,\n",
        "pattern,tier,category,note\n.gov,9,x,\n",
        "pattern,tier,category,note\n.gov,one,x,\n",
    ],
)
def test_a_bad_authority_file_is_refused(tmp_path, body):
    f = tmp_path / "a.csv"
    f.write_text(body)
    with pytest.raises(EvidenceError):
        load_authority(f)


def test_a_missing_authority_file_means_everything_is_lowest_tier(tmp_path):
    assert authority_for(load_authority(tmp_path / "none.csv"), "x.gov.in") == (4, "unclassified")


def test_official_data_outranks_wikipedia_outranks_an_unknown_blog():
    result = discover(Fake(HITS), Q, rules=RULES)
    order = [c.host for c in result.candidates]
    assert order == ["results.eci.gov.in", "en.wikipedia.org", "example-blog.com"]
    top = result.candidates[0]
    assert top.tier == 1 and top.structured and "downloadable data file" in top.reasons
    assert "host not in the authority list: treated as lowest tier" in result.candidates[2].reasons


def test_every_rejection_has_a_code_and_a_reason():
    result = discover(Fake(HITS), Q, rules=RULES)
    by_url = {r.url: r.code for r in result.rejected}
    assert by_url["http://insecure.example.org/results"] == "bad_scheme"
    assert by_url["https://localhost/admin"] == "bad_host"
    assert by_url["https://cooking.example.org/pasta"] == "irrelevant"
    assert by_url["https://results.eci.gov.in/files/2019/lok-sabha-results.csv"] == "duplicate"
    assert all(r.reason for r in result.rejected)


def test_ranking_is_deterministic_and_independent_of_provider_order():
    a = discover(Fake(HITS), Q, rules=RULES)
    b = discover(Fake(list(reversed(HITS))), Q, rules=RULES)
    assert [c.url for c in a.candidates] == [c.url for c in b.candidates]
    assert [c.score for c in a.candidates] == sorted((c.score for c in a.candidates), reverse=True)


def test_a_host_is_capped_so_one_site_cannot_fill_the_list():
    hits = [
        SearchHit(f"https://results.eci.gov.in/p{i}", "Lok Sabha results", "seats")
        for i in range(5)
    ]
    result = discover(Fake(hits), Q, rules=RULES, max_per_host=2)
    assert len(result.candidates) == 2
    assert sum(r.code == "host_cap" for r in result.rejected) == 3


def test_the_limit_is_respected_and_the_provider_is_asked_for_extra():
    p = Fake([SearchHit(f"https://h{i}.gov.in/x", "Lok Sabha results", "seats") for i in range(20)])
    assert len(discover(p, Q, rules=RULES, limit=5).candidates) == 5
    assert p.asked == [(Q, 15)]


def test_a_question_with_no_searchable_words_and_a_bad_limit_are_refused():
    with pytest.raises(EvidenceError):
        discover(Fake([]), "how many of the", rules=RULES)
    with pytest.raises(EvidenceError):
        discover(Fake([]), Q, rules=RULES, limit=0)


def test_no_hits_is_an_empty_result_not_an_error():
    result = discover(Fake([]), Q, rules=RULES)
    assert result.candidates == () and result.rejected == () and result.usable == ()


def test_terms_drop_filler_words():
    assert terms("How many seats did the BJP win") == {"seats", "did", "bjp", "win"}
    assert "the" not in terms("the seats") and "seats" in terms("the seats")


def fetcher_with(robots_by_host):
    def get(url, headers, timeout, max_bytes):
        host = url.split("/")[2]
        status, body = robots_by_host.get(host, (404, ""))
        return RawResponse(status, {"content-type": "text/plain"}, body.encode())

    t = [0.0]
    return WebFetcher(
        user_agent="SIE-research/0.1 (contact: o@example.org)", get=get, resolver=lambda h, p: ["93.184.216.34"],
        clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s),
    )  # fmt: skip


def test_the_robots_precheck_marks_forbidden_and_unreachable_hosts_without_dropping_them():
    f = fetcher_with(
        {
            "results.eci.gov.in": (200, "User-agent: *\nAllow: /\n"),
            "en.wikipedia.org": (200, "User-agent: *\nDisallow: /wiki/\n"),
            "example-blog.com": (503, ""),
        }
    )
    result = discover(Fake(HITS), Q, rules=RULES, fetcher=f)
    state = {c.host: c.allowed_by_robots for c in result.candidates}
    assert state == {
        "results.eci.gov.in": True,
        "en.wikipedia.org": False,
        "example-blog.com": False,
    }
    assert [c.host for c in result.usable] == ["results.eci.gov.in"]
    wiki = next(c for c in result.candidates if c.host == "en.wikipedia.org")
    assert "robots.txt disallows automated access" in wiki.reasons
    blog = next(c for c in result.candidates if c.host == "example-blog.com")
    assert any("not fetchable" in r for r in blog.reasons)


def test_without_a_fetcher_robots_is_reported_as_not_checked():
    assert {c.allowed_by_robots for c in discover(Fake(HITS), Q, rules=RULES).candidates} == {None}


def test_file_provider_reads_leads_and_refuses_bad_files(tmp_path):
    import json

    from sie.evidence.discovery import FileProvider

    good = tmp_path / "l.json"
    good.write_text(
        json.dumps(
            [
                {
                    "url": "https://results.eci.gov.in/a.csv",
                    "title": "Lok Sabha results",
                    "snippet": "seats",
                }
            ]
        )
    )
    result = discover(FileProvider(good), Q, rules=RULES)
    assert [c.host for c in result.candidates] == ["results.eci.gov.in"]
    for bad in ("not json", '{"url": 1}', '[{"title": "x"}]', "[1]"):
        f = tmp_path / "b.json"
        f.write_text(bad)
        with pytest.raises(EvidenceError):
            FileProvider(f).search("q", 5)
    with pytest.raises(EvidenceError):
        FileProvider(tmp_path / "missing.json").search("q", 5)


def test_geography_and_year_are_separated_from_the_topic():
    from sie.evidence.discovery import assess_lead, geographies, topic_terms, years

    q = "UK general election 2024 constituency results"
    assert topic_terms(q) == {"general", "election", "constituency", "results"}
    assert geographies("Scotland and Wales turnout") == {"united kingdom"}
    assert geographies("U.S. Census 2011") == {"united states"}
    assert geographies("Business results") == frozenset()  # 'us' must be a whole word
    assert years("Results 2024 and 1999, id 20245") == {"2024", "1999"}
    rel, limits, bad = assess_lead(q, "US general election 2011 constituency results")
    assert rel == 1.0 and len(bad) == 2  # all topic words match, yet place and year both disagree
    rel, limits, bad = assess_lead(q, "General election constituency results")
    assert bad == () and len(limits) == 2
