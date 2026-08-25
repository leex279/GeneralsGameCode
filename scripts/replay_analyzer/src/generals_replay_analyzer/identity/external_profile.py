"""Validation shared by canonical identity writes and web presentation."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

MAX_EXTERNAL_PROFILE_URL_LENGTH = 2048
MAX_EXTERNAL_PROFILE_SOURCE_LENGTH = 64


# TheSuperHackers @feature Leex 25/08/2026 Keep canonical player links HTTPS-only,
# credential-free, and safely bounded before persistence or presentation. (#TBD)
def normalize_external_profile(url: str | None, source: str | None) -> tuple[str | None, str | None]:
    if url is None and source is None:
        return None, None
    if not isinstance(url, str) or not url.strip():
        raise ValueError("external profile URL is required when source is supplied")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("external profile source is required")

    normalized_url = url.strip()
    normalized_source = source.strip()
    if len(normalized_url) > MAX_EXTERNAL_PROFILE_URL_LENGTH:
        raise ValueError("external profile URL is too long")
    if len(normalized_source) > MAX_EXTERNAL_PROFILE_SOURCE_LENGTH:
        raise ValueError("external profile source is too long")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in normalized_url):
        raise ValueError("external profile URL contains unsafe whitespace or control characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized_source):
        raise ValueError("external profile source contains control characters")

    parsed = urlsplit(normalized_url)
    hostname = parsed.hostname
    if parsed.scheme != "https" or not parsed.netloc or hostname is None or parsed.username or parsed.password:
        raise ValueError("external profile URL must be an HTTPS public URL")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("external profile URL has an invalid port") from error

    normalized_host = hostname.casefold().rstrip(".")
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise ValueError("external profile URL must use a public host")
    try:
        address = ip_address(normalized_host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("external profile URL must use a public host")
    return normalized_url, normalized_source
