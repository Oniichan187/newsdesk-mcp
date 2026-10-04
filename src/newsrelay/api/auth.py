"""Self-contained OAuth 2.1 authorization server for the MCP endpoint.

- Dynamic Client Registration restricted to allow-listed redirect URIs (ChatGPT's connector callbacks).
- PKCE (S256) enforced by the MCP SDK's token handler.
- Authorization requires the owner passphrase (scrypt hash stored as a systemd credential);
  failed attempts are rate-limited.
- Codes/tokens are random 256-bit values stored only as SHA-256 hashes; refresh tokens rotate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .. import timeutil
from ..config import Config
from ..database import tx
from ..logutil import log

logger = logging.getLogger("newsrelay.auth")

SCOPE = "newsrelay"
SUPPORTED_SCOPES = [SCOPE, "offline_access"]
CODE_TTL_S = 300
PENDING_TTL_S = 900
MAX_FAILURES = 5
FAILURE_WINDOW_S = 900
MAX_CLIENTS = 25

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        passphrase.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            passphrase.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def redirect_allowed(uri: str, allowed: tuple[str, ...]) -> bool:
    """Parsed (not string-prefix) redirect URI check.

    Each allowed entry is either an exact URL (``https://host/path``) or a prefix ending in ``/``
    that admits exactly one extra safe path segment (``https://host/connector/oauth/{id}``).
    Scheme must be https; userinfo, explicit ports, query strings, fragments, dot-segments and
    percent-escapes are rejected.
    """
    try:
        u = urlsplit(uri)
        port = u.port
    except ValueError:
        return False
    if u.scheme != "https" or u.username or u.password or port is not None or u.query or u.fragment:
        return False
    if "%" in uri or "\\" in uri or not u.hostname:
        return False
    for entry in allowed:
        e = urlsplit(entry)
        if u.hostname != e.hostname:
            continue
        if entry.endswith("/"):
            rest = u.path[len(e.path) :] if u.path.startswith(e.path) else None
            if rest is not None and _SEGMENT_RE.match(rest):
                return True
        elif u.path == e.path:
            return True
    return False


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SqliteOAuthProvider:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, passphrase_hash: str | None) -> None:
        self.conn = conn
        self.cfg = cfg
        self.passphrase_hash = passphrase_hash

    # ---- clients ---------------------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.conn.execute(
            "SELECT client_info FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u, self.cfg.allowed_redirect_prefixes) for u in uris):
            log(logger, logging.WARNING, "client registration refused: redirect URI not allow-listed")
            raise RegistrationError("invalid_redirect_uri", "redirect_uri not allowed by this server")
        with tx(self.conn):
            n = self.conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
            if n >= MAX_CLIENTS:
                self.conn.execute(
                    "DELETE FROM oauth_clients WHERE client_id IN (SELECT client_id FROM oauth_clients "
                    "WHERE client_id NOT IN (SELECT DISTINCT client_id FROM oauth_tokens WHERE revoked = 0) "
                    "ORDER BY COALESCE(last_used_at, created_at) LIMIT ?)",
                    (n - MAX_CLIENTS + 1,),
                )
            self.conn.execute(
                "INSERT INTO oauth_clients(client_id, client_info, created_at) VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), timeutil.now_iso()),
            )
        log(
            logger,
            logging.INFO,
            "oauth client registered",
            client_id=client_info.client_id,
            client_name=client_info.client_name,
        )

    # ---- authorization ---------------------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_allowed(str(params.redirect_uri), self.cfg.allowed_redirect_prefixes):
            raise AuthorizeError("invalid_request", "redirect_uri not allowed")
        if params.resource and params.resource.rstrip("/") != self.cfg.mcp_resource_url:
            raise AuthorizeError("invalid_target", "unknown resource")
        pending_id = secrets.token_urlsafe(32)
        with tx(self.conn):
            self.conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (time.time(),))
            self.conn.execute(
                "INSERT INTO oauth_pending(id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (pending_id, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL_S),
            )
        return f"{self.cfg.public_base_url.rstrip('/')}/oauth/consent?req={pending_id}"

    def pending_request(
        self, pending_id: str
    ) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        row = self.conn.execute(
            "SELECT client_id, params FROM oauth_pending WHERE id = ? AND expires_at > ?",
            (pending_id, time.time()),
        ).fetchone()
        if row is None:
            return None
        client_row = self.conn.execute(
            "SELECT client_info FROM oauth_clients WHERE client_id = ?", (row["client_id"],)
        ).fetchone()
        if client_row is None:
            return None
        return (
            OAuthClientInformationFull.model_validate_json(client_row[0]),
            AuthorizationParams.model_validate_json(row["params"]),
        )

    def locked_out(self) -> bool:
        n = self.conn.execute(
            "SELECT count(*) FROM auth_failures WHERE ts > ?", (time.time() - FAILURE_WINDOW_S,)
        ).fetchone()[0]
        return bool(n >= MAX_FAILURES)

    def approve(self, pending_id: str, passphrase: str) -> str | None:
        """Verify the owner passphrase; on success return the redirect URL carrying the code."""
        if self.passphrase_hash is None or self.locked_out():
            return None
        req = self.pending_request(pending_id)
        if req is None:
            return None
        if not verify_passphrase(passphrase, self.passphrase_hash):
            with tx(self.conn):
                self.conn.execute("INSERT INTO auth_failures(ts) VALUES (?)", (time.time(),))
                self.conn.execute("DELETE FROM auth_failures WHERE ts < ?", (time.time() - 86400,))
            log(logger, logging.WARNING, "oauth consent: wrong passphrase")
            return None
        client, params = req
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(
            code="",
            scopes=params.scopes or [SCOPE, "offline_access"],
            expires_at=time.time() + CODE_TTL_S,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        with tx(self.conn):
            self.conn.execute("DELETE FROM oauth_pending WHERE id = ?", (pending_id,))
            self.conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (time.time(),))
            self.conn.execute(
                "INSERT INTO oauth_codes(code_hash, data, expires_at) VALUES (?, ?, ?)",
                (_h(code), data.model_dump_json(), data.expires_at),
            )
        log(logger, logging.INFO, "oauth consent granted", client_id=client.client_id)
        # RFC 9207: `iss` lets ChatGPT use its stable redirect URI and prevents mix-up attacks.
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state, iss=self.cfg.public_base_url.rstrip("/")
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.conn.execute(
            "SELECT data FROM oauth_codes WHERE code_hash = ? AND expires_at > ?",
            (_h(authorization_code), time.time()),
        ).fetchone()
        if row is None:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        if code.client_id != client.client_id:
            return None
        return code.model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with tx(self.conn):
            cur = self.conn.execute(
                "DELETE FROM oauth_codes WHERE code_hash = ?", (_h(authorization_code.code),)
            )
            if cur.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code already used")
            token = self._issue(
                client.client_id, authorization_code.scopes, authorization_code.resource, secrets.token_hex(8)
            )
        return token

    # ---- tokens ----------------------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str], resource: str | None, grant_id: str) -> OAuthToken:
        """Insert a fresh access+refresh pair (caller holds the transaction)."""
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now_i = int(time.time())
        a_exp = now_i + self.cfg.access_token_ttl_s
        r_exp = now_i + self.cfg.refresh_token_ttl_days * 86400
        t = timeutil.now_iso()
        for tok, kind, exp in ((access, "access", a_exp), (refresh, "refresh", r_exp)):
            self.conn.execute(
                "INSERT INTO oauth_tokens(token_hash, kind, client_id, scopes, resource, grant_id, expires_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_h(tok), kind, client_id, json.dumps(scopes), resource, grant_id, exp, t),
            )
        self.conn.execute("UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?", (t, client_id))
        self.conn.execute(
            "DELETE FROM oauth_tokens WHERE expires_at < ? OR (revoked = 1 AND created_at < ?)",
            (now_i - 86400, timeutil.iso_plus(-30 * 86400)),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106
            expires_in=self.cfg.access_token_ttl_s,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _row(self, token: str, kind: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND revoked = 0 AND "
            "(expires_at IS NULL OR expires_at > ?)",
            (_h(token), kind, int(time.time())),
        ).fetchone()
        return row

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._row(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        with tx(self.conn):
            row = self._row(refresh_token.token, "refresh")
            if row is None:
                raise TokenError("invalid_grant", "refresh token invalid")
            granted = json.loads(row["scopes"])
            if scopes and not set(scopes) <= set(granted):
                raise TokenError("invalid_scope", "cannot widen scope")
            # Rotate: revoke the whole previous pair of this grant, issue a new pair.
            self.conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE grant_id = ?", (row["grant_id"],))
            token = self._issue(client.client_id, scopes or granted, row["resource"], row["grant_id"])
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._row(token, "access")
        if row is None:
            return None
        if row["resource"] and row["resource"].rstrip("/") != self.cfg.mcp_resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self.conn.execute(
            "SELECT grant_id FROM oauth_tokens WHERE token_hash = ?", (_h(token.token),)
        ).fetchone()
        if row:
            with tx(self.conn):
                self.conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE grant_id = ?", (row[0],))

    def revoke_all(self) -> int:
        with tx(self.conn):
            return self.conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE revoked = 0").rowcount
