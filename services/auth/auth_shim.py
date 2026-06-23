"""Axonate auth shim — verify identity and map it to a LiteLLM virtual key.

Two modes (env AUTH_MODE):
  dev        — no SSO. Identity is ROUTER_DEV_USER. For localhost development only.
  cloudflare — VERIFY the Cloudflare Access JWT against the team's public keys (JWKS).
               Headers are NOT trusted; the signature + aud + exp are checked.

Imported by the router. Never trust a plain header for identity in cloudflare mode.
"""
from __future__ import annotations

import os
import time

import httpx
import yaml
from jose import jwt
from jose.exceptions import JWTError

AUTH_MODE = os.environ.get("AUTH_MODE", "dev")
DEV_USER = os.environ.get("ROUTER_DEV_USER", "dev@local")
USERS_FILE = os.environ.get("USERS_FILE", "/app/users.yaml")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

CF_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "")
CF_CERTS_URL = f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs" if CF_TEAM_DOMAIN else ""

# Cloudflare Access injects the signed identity in this header.
CF_JWT_HEADER = "cf-access-jwt-assertion"


class AuthError(Exception):
    """Identity could not be verified or mapped. Router turns this into 401/403."""


def _load_users() -> dict[str, str]:
    """email -> litellm virtual key. Missing file => empty map (dev falls back to master)."""
    try:
        with open(USERS_FILE) as f:
            data = yaml.safe_load(f) or {}
        return {str(k).lower(): str(v) for k, v in (data.get("users") or {}).items()}
    except FileNotFoundError:
        return {}


_jwks_cache: dict = {"keys": None, "fetched": 0.0}
_JWKS_TTL = 3600


def _get_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched"] < _JWKS_TTL:
        return _jwks_cache["keys"]
    resp = httpx.get(CF_CERTS_URL, timeout=10)
    resp.raise_for_status()
    _jwks_cache["keys"] = resp.json()
    _jwks_cache["fetched"] = now
    return _jwks_cache["keys"]


def _verify_cf_jwt(token: str) -> str:
    """Return the verified email, or raise AuthError."""
    if not CF_TEAM_DOMAIN or not CF_AUD:
        raise AuthError("cloudflare mode misconfigured: set CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD")
    try:
        jwks = _get_jwks()
        claims = jwt.decode(
            token, jwks, algorithms=["RS256"], audience=CF_AUD,
            options={"verify_aud": True, "verify_exp": True},
        )
    except (JWTError, httpx.HTTPError) as e:
        raise AuthError(f"invalid Access JWT: {e}") from e
    email = claims.get("email")
    if not email:
        raise AuthError("Access JWT has no email claim")
    return email.lower()


def resolve_identity(headers) -> tuple[str, str]:
    """Return (email, virtual_key). Raise AuthError if unverifiable/unmapped.

    `headers` is any case-insensitive mapping (e.g. Starlette request.headers).
    """
    users = _load_users()

    if AUTH_MODE == "dev":
        email = DEV_USER.lower()
        key = users.get(email) or MASTER_KEY
        if not key:
            raise AuthError("dev mode: no virtual key for dev user and no master key set")
        return email, key

    if AUTH_MODE == "cloudflare":
        token = headers.get(CF_JWT_HEADER)
        if not token:
            raise AuthError("missing Cloudflare Access JWT")
        email = _verify_cf_jwt(token)
        key = users.get(email)
        if not key:
            raise AuthError(f"user {email} has no provisioned virtual key")
        return email, key

    raise AuthError(f"unknown AUTH_MODE '{AUTH_MODE}'")
