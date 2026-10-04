"""URL canonicalization and Unicode-aware text normalization."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "_ga",
    "_gl",
    "ref_src",
    "ref_url",
    "cmpid",
    "wt_mc",
    "wt.mc_id",
    "ito",
    "ns_mchannel",
    "ns_source",
    "ns_campaign",
    "ns_linkname",
    "ns_fee",
    "spm",
    "share",
    "smid",
    "rss",
    "xtor",
    "at_medium",
    "at_campaign",
    "at_custom1",
    "at_custom2",
    "at_custom3",
    "at_custom4",
    "src",
    "s_cid",
    "icid",
    "ocid",
    "vero_id",
    "oly_enc_id",
    "oly_anon_id",
    "__twitter_impression",
    "trk",
    "si",
    "mkt_tok",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "at_", "wt_", "oly_", "__hs")

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_EXACT or k.startswith(_TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """Normalize a URL for identity comparison.

    Lowercases scheme/host, drops 'www.', default ports, fragments, tracking parameters, and a
    trailing slash; sorts remaining (meaningful) query parameters.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port is None or str(port) == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = quote(unquote(parts.path), safe="/:@!$&'()*+,;=-._~%")
    path = re.sub(r"/{2,}", "/", path)
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def url_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Small multilingual stopword list (EN/DE) used only for fingerprints and FTS queries.
STOPWORDS = frozenset(
    (
        "a an the and or of to in on for at by with from as is are was were be been has have had it its "
        "this that these those after before over under into about new says said update report reports "
        "der die das den dem des ein eine einer eines und oder zu im am auf für mit von bei nach aus "
        "ist sind war wurde wurden hat haben wird werden nicht auch als wie über unter vor"
    ).split()
)


def normalize_text(text: str) -> str:
    """NFKC + casefold + strip diacritics/punctuation + collapse whitespace."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("ß", "ss")
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def tokens(text: str, *, drop_stopwords: bool = True) -> list[str]:
    toks = normalize_text(text).split()
    if drop_stopwords:
        toks = [t for t in toks if t not in STOPWORDS and len(t) > 1]
    return toks


def slugify(text: str, max_len: int = 60) -> str:
    slug = "-".join(tokens(text))[:max_len].strip("-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return slug or "topic"
