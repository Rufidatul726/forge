import hmac
import time
from typing import Any

import httpx
import jwt
from jwt import InvalidTokenError

from forge.config import ForgeSettings

_JWKS_CACHE: dict[str, tuple[float, jwt.PyJWKSet]] = {}
_JWKS_TTL_SECONDS = 300


class AuthenticationError(Exception):
    pass


class InsufficientScopeError(AuthenticationError):
    def __init__(self, message: str = "insufficient_scope") -> None:
        super().__init__(message)


class InsufficientRoleError(AuthenticationError):
    def __init__(self, message: str = "insufficient_role") -> None:
        super().__init__(message)


def clear_jwks_cache() -> None:
    _JWKS_CACHE.clear()


async def authenticate_bearer(
    token: str,
    settings: ForgeSettings,
) -> None:
    if settings.auth_mode == "api_key":
        configured_key = settings.forge_api_key
        if configured_key is None:
            raise AuthenticationError("api_key_not_configured")
        if not hmac.compare_digest(
            token.encode("utf-8"),
            configured_key.get_secret_value().encode("utf-8"),
        ):
            raise AuthenticationError("invalid_token")
        return

    if settings.auth_mode == "api_key_or_oidc":
        configured_key = settings.forge_api_key
        if configured_key is not None and hmac.compare_digest(
            token.encode("utf-8"),
            configured_key.get_secret_value().encode("utf-8"),
        ):
            return

    oidc_error: AuthenticationError | None = None
    if settings.auth_mode in {"oidc", "api_key_or_oidc"}:
        try:
            await _authenticate_oidc(token, settings)
            return
        except AuthenticationError as exc:
            oidc_error = exc

    raise oidc_error or AuthenticationError("invalid_token")


async def _authenticate_oidc(token: str, settings: ForgeSettings) -> None:
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise AuthenticationError("oidc_not_configured")

    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise AuthenticationError("invalid_token") from exc

    jwks = await _get_jwks(settings)
    kid = header.get("kid")
    signing_key = None
    for key in jwks.keys:
        if kid is None or key.key_id == kid:
            signing_key = key.key
            break

    if signing_key is None and kid is not None:
        jwks = await _get_jwks(settings, force_refresh=True)
        for key in jwks.keys:
            if key.key_id == kid:
                signing_key = key.key
                break

    if signing_key is None:
        raise AuthenticationError("invalid_token")

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
        )
    except InvalidTokenError as exc:
        raise AuthenticationError("invalid_token") from exc

    if settings.oidc_required_scope:
        granted: set[str] = set()
        raw_scope = claims.get("scope")
        if isinstance(raw_scope, str):
            granted.update(raw_scope.split())
        elif isinstance(raw_scope, list):
            granted.update(s for s in raw_scope if isinstance(s, str))

        raw_scp = claims.get("scp")
        if isinstance(raw_scp, str):
            granted.update(raw_scp.split())
        elif isinstance(raw_scp, list):
            granted.update(s for s in raw_scp if isinstance(s, str))

        required = set(settings.oidc_required_scope.split())
        if not required.issubset(granted):
            raise InsufficientScopeError("insufficient_scope")

    if settings.oidc_required_role:
        roles = _extract_roles(claims)
        if settings.oidc_required_role not in roles:
            raise InsufficientRoleError("insufficient_role")


def _extract_roles(claims: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    for key in ("roles", "role", "groups"):
        val = claims.get(key)
        if isinstance(val, list):
            roles.update(r for r in val if isinstance(r, str))
        elif isinstance(val, str):
            roles.add(val)

    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        realm_roles = realm.get("roles", [])
        if isinstance(realm_roles, list):
            roles.update(role for role in realm_roles if isinstance(role, str))

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        for resource in resource_access.values():
            if not isinstance(resource, dict):
                continue
            resource_roles = resource.get("roles", [])
            if isinstance(resource_roles, list):
                roles.update(role for role in resource_roles if isinstance(role, str))
    return roles


async def _get_jwks(settings: ForgeSettings, *, force_refresh: bool = False) -> jwt.PyJWKSet:
    if not settings.oidc_issuer:
        raise AuthenticationError("oidc_not_configured")

    jwks_url = settings.oidc_jwks_url
    if jwks_url is None:
        discovery_url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(discovery_url)
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise AuthenticationError("oidc_discovery_failed") from exc
        jwks_url = document.get("jwks_uri")
        if not isinstance(jwks_url, str):
            raise AuthenticationError("oidc_discovery_failed")

    now = time.monotonic()
    cached = _JWKS_CACHE.get(jwks_url)
    if not force_refresh and cached is not None and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(jwks_url)
            response.raise_for_status()
            jwks_data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthenticationError("oidc_discovery_failed") from exc
    try:
        jwks = jwt.PyJWKSet.from_dict(jwks_data)
    except (InvalidTokenError, ValueError, TypeError) as exc:
        raise AuthenticationError("oidc_discovery_failed") from exc
    _JWKS_CACHE[jwks_url] = (now, jwks)
    return jwks
