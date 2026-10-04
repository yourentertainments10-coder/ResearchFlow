import pytest

from sie.reference import normalise_key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("India", "india"),
        ("  INDIA  ", "india"),
        ("India 🇮🇳", "india"),
        ("Hong Kong, China", "hong kong china"),
        ("Hong Kong China", "hong kong china"),
        ("Men's", "mens"),
        ("Women’s", "womens"),  # typographic apostrophe
        ("Ｔｒａｃｋ　ａｎｄ　Ｆｉｅｌｄ", "track and field"),  # full-width forms (NFKC)
        ("Track & Field", "track field"),
        ("Table-Tennis", "table tennis"),
        ("Timor-Leste", "timor leste"),
        ("Timor Leste", "timor leste"),
        ("A   B", "a b"),
    ],
)
def test_normalise_key(raw, key):
    assert normalise_key(raw) == key


def test_empty_after_normalisation():
    assert normalise_key("🇮🇳 !!") == ""
