"""Free-source research (ADR-045, Phase 4c): discover, fetch, cache, collect, analyse, report.

Everything here uses free access only. There is no paid endpoint anywhere: a catalogue entry must declare
``free_no_key`` access or it is refused when loaded, provider calls can only reach registered hosts, and a
provider that runs out of quota, times out or fails is reported and skipped, never replaced by anything
that costs money. Fetching goes through ``sie.evidence.web`` (robots.txt, rate limit, SSRF guard); parsing
and tables go through ``sie.datasets``. No model is used and no number comes from prose.
"""
