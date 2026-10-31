"""HTTP Basic and Bearer checks. Comparisons are constant-time and fail closed.

Basic auth checks BASIC_AUTH_PASS_SHA256 when that value is set: the
lowercase hex SHA-256 of the UTF-8 password. BASIC_AUTH_PASS is then
optional. If both are set, the hash is the check that must succeed.
"""
import base64
import hashlib
import hmac


def const_eq(left, right):
    """Compare two byte strings without leaking their contents through timing.

    Both sides are hashed first so the comparison length does not depend on
    the input length. hashlib and hmac are the standard library.
    """
    if not isinstance(left, (bytes, bytearray)):
        left = b""
    if not isinstance(right, (bytes, bytearray)):
        right = b""
    return hmac.compare_digest(
        hashlib.sha256(bytes(left)).digest(),
        hashlib.sha256(bytes(right)).digest(),
    )


def basic_ok(header, user, password="", password_sha256=""):
    """Return True only when header carries the expected user and password.

    An empty password never succeeds, even if the client sends an empty one.
    A malformed header still runs the comparison so the failure path does not
    return early on a parse error alone.

    password_sha256, when non-empty, is the lowercase hex SHA-256 of the
    UTF-8 password. It replaces the plaintext check. The presented password
    is hashed and compared in constant time. The plaintext is not consulted.
    """
    presented_user = b""
    presented_password = b""
    if isinstance(header, str) and header.startswith("Basic "):
        token = header[6:].strip()
        try:
            raw = base64.b64decode(token, validate=True)
        except Exception:
            raw = b""
        if b":" in raw:
            presented_user, presented_password = raw.split(b":", 1)
    user_match = const_eq(presented_user, (user or "").encode("utf-8"))
    expected_hash = (password_sha256 or "").strip().lower()
    if expected_hash:
        digest = hashlib.sha256(presented_password).hexdigest()
        return user_match and const_eq(digest.encode("ascii"), expected_hash.encode("ascii"))
    password_match = const_eq(presented_password, (password or "").encode("utf-8"))
    if not password:
        return False
    return user_match and password_match


def bearer_ok(header, token):
    """Return True only when header is Bearer and matches token.

    An empty token never succeeds.
    """
    presented = b""
    if isinstance(header, str):
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            presented = parts[1].strip().encode("utf-8")
    if not token:
        const_eq(presented, b"")
        return False
    return const_eq(presented, token.encode("utf-8"))


def origin_allowed(origin, host, forwarded_proto, fetch_site=""):
    """Allow missing Origin (curl) and same-host browser Origins only.

    WebKit sends Origin: null on a same-origin form POST. That is allowed
    unless the browser also says the fetch site is cross-site. A normal
    cross-site form still sends the other site's origin and is refused.
    """
    site = (fetch_site or "").split(",")[0].strip().lower()
    if not origin or origin == "null":
        return site != "cross-site"
    if not host:
        return False
    host = host.split(",")[0].strip()
    schemes = {"https", "http"}
    if forwarded_proto:
        schemes.add(forwarded_proto.split(",")[0].strip().lower())
    return any(origin == f"{scheme}://{host}" for scheme in schemes if scheme)
