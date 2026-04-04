"""Token generation and validation for CLI <-> worker auth."""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "ghb_tok_"
TOKEN_BYTES = 32  # 256-bit token


def generate_token() -> str:
    """Generate a new bearer token with the ghb_tok_ prefix."""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)


def constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def validate_token(provided: str, expected: str) -> bool:
    """Validate a bearer token against the expected value.

    Returns True if valid, False otherwise. Uses constant-time comparison.
    """
    if not provided or not expected:
        return False
    return constant_time_compare(provided, expected)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract the token from an 'Authorization: Bearer <token>' header.

    Returns None if the header is missing or malformed.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    return token


def hash_token_for_logging(token: str) -> str:
    """Return a truncated SHA-256 hash of a token, safe for logging."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:16] + "..."
