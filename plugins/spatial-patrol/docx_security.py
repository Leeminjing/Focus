"""Small scoped HMAC tokens for the isolated bridge (not Focus authentication)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Iterable


class TokenError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class ScopedClaims:
    session_id: str
    document_id: str
    scopes: frozenset[str]
    expires_at: int


class ScopedTokenSigner:
    def __init__(self, secret: str | bytes) -> None:
        self._secret = secret.encode() if isinstance(secret, str) else secret
        if len(self._secret) < 32:
            raise ValueError("bridge token secret must be at least 32 bytes")

    def issue(
        self,
        *,
        session_id: str,
        document_id: str,
        scopes: Iterable[str],
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> str:
        issued = int(time.time() if now is None else now)
        payload = {
            "sid": session_id,
            "did": document_id,
            "scp": sorted(set(scopes)),
            "exp": issued + ttl_seconds,
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64encode(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(
        self,
        token: str,
        *,
        required_scope: str,
        session_id: str | None = None,
        document_id: str | None = None,
        now: int | None = None,
    ) -> ScopedClaims:
        try:
            body, supplied_signature = token.split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise TokenError("invalid token signature")
            raw = json.loads(_b64decode(body))
            claims = ScopedClaims(
                session_id=str(raw["sid"]),
                document_id=str(raw["did"]),
                scopes=frozenset(str(item) for item in raw["scp"]),
                expires_at=int(raw["exp"]),
            )
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError("malformed token") from exc
        current = int(time.time() if now is None else now)
        if claims.expires_at < current:
            raise TokenError("expired token")
        if required_scope not in claims.scopes:
            raise TokenError("token scope denied")
        if session_id is not None and not hmac.compare_digest(claims.session_id, session_id):
            raise TokenError("token session mismatch")
        if document_id is not None and not hmac.compare_digest(claims.document_id, document_id):
            raise TokenError("token document mismatch")
        return claims
