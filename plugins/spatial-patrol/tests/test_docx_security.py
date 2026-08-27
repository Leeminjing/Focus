import base64

from plugins.spatial_patrol.docx_security import ScopedTokenSigner, TokenError


def test_scoped_token_accepts_only_bound_scope_session_and_document():
    signer = ScopedTokenSigner("s" * 48)
    token = signer.issue(
        session_id="session-a", document_id="document-a",
        scopes={"document:read"}, ttl_seconds=60, now=100,
    )
    claims = signer.verify(
        token, required_scope="document:read", session_id="session-a",
        document_id="document-a", now=101,
    )
    assert claims.session_id == "session-a"
    for kwargs in (
        {"required_scope": "callback:write"},
        {"required_scope": "document:read", "session_id": "session-b"},
        {"required_scope": "document:read", "document_id": "document-b"},
        {"required_scope": "document:read", "now": 161},
    ):
        try:
            signer.verify(token, **kwargs)
            raise AssertionError("token should have been rejected")
        except TokenError:
            pass


def test_scoped_token_rejects_tampering():
    signer = ScopedTokenSigner("s" * 48)
    token = signer.issue(
        session_id="session-a", document_id="document-a",
        scopes={"document:read"}, now=100,
    )
    body, signature = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[-2] ^= 1
    tampered = base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + signature
    try:
        signer.verify(tampered, required_scope="document:read", now=101)
        raise AssertionError("tampered token should have been rejected")
    except TokenError:
        pass
