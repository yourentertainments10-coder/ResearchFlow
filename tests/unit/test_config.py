import pytest

from sie.config import Settings, normalise_database_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        (
            "postgresql://u:p@host/db?sslmode=require",
            "postgresql+psycopg://u:p@host/db?sslmode=require",
        ),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("  postgres://u:p@host/db  ", "postgresql+psycopg://u:p@host/db"),
    ],
)
def test_database_url_forms_from_hosting_providers(given, expected):
    assert normalise_database_url(given) == expected


@pytest.mark.parametrize("bad", ["mysql://x", "sqlite:///x.db", "mongodb://x", ""])
def test_unsupported_database_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        normalise_database_url(bad)


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://a:b@h/d")
    monkeypatch.setenv("SOURCE_PRIORITY", "official, manual ,other")
    monkeypatch.setenv("HTTP_MIN_INTERVAL_SECONDS", "3.5")
    s = Settings()
    assert s.database_url == "postgresql+psycopg://a:b@h/d"
    assert s.source_priority_list == ["official", "manual", "other"]
    assert s.http_min_interval_seconds == 3.5
