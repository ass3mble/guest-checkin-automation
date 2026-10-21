"""Access control helpers. Unconfigured credentials fail closed by default."""

from __future__ import annotations

import hmac
import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException


def require_token(expected: str, supplied: str | None, *, purpose: str) -> None:
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"{purpose} is not configured; set the corresponding secret before use.",
        )
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized.")


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, credential = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and credential and credential.strip() == credential:
        return credential
    return None


def validated_download_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Constrain document downloads to explicitly configured HTTPS hostnames.

    All redirects must be validated separately. The allowlist is intentionally
    conservative because Jotform uploads are attacker-controlled inputs.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid document URL port.") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or any(char.isspace() for char in url)
    ):
        raise ValueError("Document URLs must use HTTPS with an approved hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Direct IP-address document URLs are not permitted.")
    if not any(
        allowed and allowed != "*" and
        (host == allowed.strip().lower() or host.endswith("." + allowed.strip().lower()))
        for allowed in allowed_hosts
    ):
        raise ValueError("Document download hostname is not allowlisted.")
    return url
