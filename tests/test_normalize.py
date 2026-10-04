from newsrelay.dedup.fingerprint import facts_fingerprint, title_fingerprint, url_hash
from newsrelay.dedup.normalize import canonical_url, normalize_text, slugify


def test_canonical_url_lowercases_host_and_drops_fragment_and_www():
    assert canonical_url("https://WWW.Example.COM/a/b/#section") == "https://example.com/a/b"


def test_tracking_parameters_removed_meaningful_kept_and_sorted():
    url = "https://example.com/article?id=42&utm_source=x&fbclid=abc&gclid=1&page=2&utm_medium=rss&mc_cid=9"
    assert canonical_url(url) == "https://example.com/article?id=42&page=2"


def test_trailing_slash_and_default_port_and_duplicate_slashes():
    assert canonical_url("https://example.com:443//news//item/") == "https://example.com/news/item"
    assert canonical_url("https://example.com/") == "https://example.com"


def test_idn_host_normalized():
    assert canonical_url("https://Übermedien.de/x") == canonical_url("https://xn--bermedien-p9a.de/x")


def test_url_hash_equal_for_equivalent_urls():
    assert url_hash("https://www.taz.de/!5900000/?utm_campaign=a") == url_hash("https://taz.de/!5900000")


def test_unicode_normalization_and_casefold():
    assert normalize_text("Straße — ÜBER „Café“") == "strasse uber cafe"
    assert normalize_text("ｆｕｌｌｗｉｄｔｈ") == "fullwidth"  # NFKC
    assert normalize_text("é") == normalize_text("é")


def test_title_fingerprint_order_and_case_insensitive():
    assert title_fingerprint("EU fines Meta over Marketplace") == title_fingerprint(
        "marketplace: Meta fines EU!"
    )
    assert title_fingerprint("EU fines Meta") != title_fingerprint("EU fines Google")


def test_fact_fingerprint_order_insensitive_and_sensitive_to_content():
    a = ["Fact one is here", "Second fact"]
    assert facts_fingerprint(a) == facts_fingerprint(list(reversed(a)))
    assert facts_fingerprint(a) == facts_fingerprint(["fact ONE is here.", "second fact"])
    assert facts_fingerprint(a) != facts_fingerprint([*a, "A new third fact"])


def test_slugify():
    assert slugify("EU fines Meta €798m over Marketplace!") == "eu-fines-meta-798m-marketplace"
