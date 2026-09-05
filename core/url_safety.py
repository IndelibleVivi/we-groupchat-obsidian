"""Canonical URL redaction for every display and export surface.

Exact observed URLs remain the responsibility of their private canonical
ledgers. This module owns only the value that may be shown to a person, sent
to an AI provider, written to a projection, or included in an exported
receipt/catalog.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


URL_RE = re.compile(
    r"https?://[^\s<>'\"，。；：！？、（）()【】\[\]{}]+",
    flags=re.IGNORECASE,
)
REDACTED_VALUE = "REDACTED"
INVALID_URL_VALUE = "REDACTED_INVALID_URL"

_SENSITIVE_COMPACT_KEYS = frozenset({
    "accesstoken",
    "apikey",
    "auth",
    "authorization",
    "authtoken",
    "bearer",
    "clientsecret",
    "code",
    "cookie",
    "credential",
    "credentials",
    "googleaccessid",
    "idtoken",
    "jwt",
    "key",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "securitytoken",
    "session",
    "sessionid",
    "sessiontoken",
    "sig",
    "signature",
    "token",
    "xamzcredential",
    "xamzsecuritytoken",
    "xamzsignature",
    "xgoogcredential",
    "xgoogsecuritytoken",
    "xgoogsignature",
})

# Short names such as ``sp`` or ``se`` are redacted only when the same
# component is recognizably an Azure SAS credential.
_AZURE_SAS_COMPACT_KEYS = frozenset({
    "rscc",
    "rscd",
    "rsce",
    "rscl",
    "rsct",
    "se",
    "si",
    "sig",
    "sip",
    "ske",
    "skoid",
    "sks",
    "skt",
    "sktid",
    "skv",
    "sp",
    "spr",
    "sr",
    "srt",
    "ss",
    "st",
    "sv",
})


def _compact_key(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def sensitive_url_key(value) -> bool:
    """Return whether one query/fragment key carries credential material."""
    compact = _compact_key(value)
    if not compact:
        return False
    if compact in _SENSITIVE_COMPACT_KEYS:
        return True
    # Preserve word boundaries before case folding: shareToken, JWTToken,
    # download_token and my-secret are credential names too. Matching whole
    # components avoids redacting ordinary words such as tokenization.
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value or ""))
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words)
    components = set(re.findall(r"[a-z0-9]+", words.casefold()))
    if any(re.fullmatch(r"(?:token|secret|jwt)[0-9]*", word) for word in components):
        return True
    if re.search(r"(?:token|secret|jwt)[0-9]*$", compact):
        return True
    return any(
        marker in compact
        for marker in (
            "accesstoken",
            "apikey",
            "authorization",
            "authtoken",
            "credential",
            "password",
            "refreshtoken",
            "securitytoken",
            "sessionid",
            "sessiontoken",
            "signature",
        )
    )


def _redact_parameter_text(value: str) -> tuple[str, bool]:
    pairs = parse_qsl(value, keep_blank_values=True)
    if not pairs:
        return value, False
    compact_keys = [_compact_key(key) for key, _item in pairs]
    azure_sas = (
        "sig" in compact_keys
        and any(
            key in _AZURE_SAS_COMPACT_KEYS and key != "sig"
            for key in compact_keys
        )
    )
    changed = False
    redacted = []
    for (key, item), compact in zip(pairs, compact_keys):
        hide = sensitive_url_key(key) or (
            azure_sas and compact in _AZURE_SAS_COMPACT_KEYS
        )
        if hide:
            redacted.append((key, REDACTED_VALUE))
            changed = True
        else:
            redacted.append((key, item))
    if not changed:
        return value, False
    return urlencode(redacted, doseq=True), True


def _redact_fragment(fragment: str) -> str:
    if not fragment:
        return ""
    decoded = unquote(fragment)
    prefix = ""
    parameters = decoded
    if "?" in decoded:
        route, parameters = decoded.split("?", 1)
        prefix = route + "?"
    elif "=" not in decoded:
        return fragment
    redacted, changed = _redact_parameter_text(parameters)
    return prefix + redacted if changed else fragment


def redact_url_for_display(url) -> str:
    """Return a fail-closed HTTP(S) URL safe for display or export."""
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return INVALID_URL_VALUE
        # These properties validate malformed ports and NFKC host delimiters.
        _ = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError):
        return INVALID_URL_VALUE
    if any(ord(char) < 32 for char in raw):
        return INVALID_URL_VALUE

    netloc = parsed.netloc
    if username is not None or password is not None:
        netloc = REDACTED_VALUE + "@" + netloc.rsplit("@", 1)[-1]

    query, _changed = _redact_parameter_text(parsed.query)
    fragment = _redact_fragment(parsed.fragment)
    return urlunsplit(parsed._replace(
        netloc=netloc,
        query=query,
        fragment=fragment,
    ))


def redact_urls_in_text(value) -> str:
    """Redact every HTTP(S) URL embedded in one display/log/prompt string."""
    text = str(value or "")
    return URL_RE.sub(
        lambda match: redact_url_for_display(match.group(0)),
        text,
    )
