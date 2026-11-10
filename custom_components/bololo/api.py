"""Async client for the Bololo AEP API recovered from the Android app.

The app's modules 1161, 1221, 1222 and 1271 document the wire format.
In particular, control requests wrap a JSON *string* in the AEP envelope.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import aiohttp

BASE_URL = "https://app.bololoapp3.com/"
APP_ID = "91732ca20dfd4517b164c38aac162c43"
_MISSING = object()
_AUTH_CODES = {505, 526}
_LOGIN_AUTH_CODES = {504, 1000000, 1000033}


class BololoError(Exception):
    """An invalid or unsuccessful cloud response."""


class BololoAuthError(BololoError):
    """Credentials or a session were rejected."""


class BololoConnectionError(BololoError):
    """The cloud could not be reached."""


class BololoClient:
    """Use a caller-owned session; never log credentials or server payloads."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str = "",
        password: str = "",
        country_code: str = "86",
        token: str | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._country_code = country_code
        self._token = token
        self._uid: str | None = None
        self._refresh_token: str | None = None
        self._created_at: float | None = None
        self._expired_at: float | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def uid(self) -> str | None:
        """The stable account ID returned by login."""
        return self._uid

    @property
    def token(self) -> str | None:
        """The current token (which may rotate during refresh)."""
        return self._token

    async def async_login(self) -> None:
        """Authenticate explicitly with the configured account and password."""
        async with self._auth_lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        if not self._username or not self._password:
            raise BololoAuthError("An account and password are required")
        try:
            data = await self._raw_request(
                "POST",
                "app/smart_home/login/pwd",
                {
                    "account": self._username,
                    "password": self._password,
                    "mobileCountryCode": self._country_code,
                    "lang": "zh",
                    "refreshToken": True,
                },
                authenticated=False,
                login=True,
            )
        except BololoAuthError:
            self._token = None
            raise
        if not isinstance(data, dict):
            raise BololoError("Invalid login response")
        authentication = data.get("jwtAuthenticationDto")
        token = authentication.get("token") if isinstance(authentication, dict) else None
        if not isinstance(token, str) or not token:
            raise BololoError("Login response does not contain a token")
        self._update_auth(
            token,
            data.get("refreshToken"),
            data.get("createdAt"),
            data.get("expiredAt"),
            data.get("uid"),
        )

    def _update_auth(
        self, token: str, refresh_token: Any, created_at: Any, expired_at: Any, uid: Any
    ) -> None:
        self._token = token
        self._refresh_token = refresh_token if isinstance(refresh_token, str) else None
        self._created_at = self._timestamp(created_at)
        self._expired_at = self._timestamp(expired_at)
        if uid is not None:
            self._uid = str(uid)

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _refresh_due(self) -> bool:
        if not self._refresh_token or self._expired_at is None:
            return False
        created = self._created_at
        if created is not None and self._expired_at > created:
            return time.time() > created + (self._expired_at - created) / 2
        return time.time() >= self._expired_at - 60

    async def async_ensure_authenticated(self) -> None:
        """Serialize login and refresh across concurrent device polls."""
        async with self._auth_lock:
            if not self._token:
                await self._login_locked()
            elif self._refresh_due():
                await self._refresh_or_login_locked()

    async def _refresh_or_login_locked(self) -> None:
        if self._refresh_token and self._token:
            try:
                data = await self._raw_request(
                    "POST",
                    "app/user/refresh_token",
                    {"clientId": APP_ID, "refresh": True, "refresh_token": self._refresh_token},
                )
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("token"), str)
                    or not data["token"]
                ):
                    raise BololoError("Invalid refresh response")
                self._update_auth(
                    data["token"],
                    data.get("refresh_token"),
                    data.get("created_at"),
                    data.get("expired_at"),
                    data.get("uid"),
                )
                return
            except BololoAuthError:
                if not self._username or not self._password:
                    self._token = None
                    raise
        await self._login_locked()

    async def _read(self, method: str, path: str, body: Any = _MISSING) -> Any:
        await self.async_ensure_authenticated()
        failed_token = self._token
        try:
            return await self._raw_request(method, path, body)
        except BololoAuthError:
            async with self._auth_lock:
                # Another in-flight poll may already have refreshed this session.
                if self._token == failed_token:
                    await self._refresh_or_login_locked()
            return await self._raw_request(method, path, body)

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the AEP's flat array of devices bound to this account."""
        data = await self._read("GET", "app/smartHome/v2/users/devices")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise BololoError("Invalid device list response")
        return data

    @staticmethod
    def _device_path(device: dict[str, Any]) -> str:
        key = device.get("productKey")
        mac = device.get("mac")
        if not isinstance(key, str) or not key or not isinstance(mac, str) or not mac:
            raise BololoError("Device is missing productKey or mac")
        return f"{quote(key, safe='')}/{quote(mac, safe='')}"

    async def async_get_status(self, device: dict[str, Any]) -> dict[str, Any]:
        """Return the flat datapoint map, including any status_time metadata."""
        path = "bololo/app/device/status/" + self._device_path(device)
        data = await self._read("POST", path, {})
        if not isinstance(data, dict):
            raise BololoError("Invalid device status response")
        return data

    async def async_control(self, device: dict[str, Any], attrs: dict[str, Any]) -> None:
        """Send once only: a timed-out physical command must not be replayed."""
        if not isinstance(attrs, dict) or not attrs:
            raise BololoError("A non-empty datapoint map is required")
        path = "bololo/app/device/control/" + self._device_path(device)
        await self.async_ensure_authenticated()
        await self._raw_request(
            "POST", path, json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
        )

    async def _raw_request(
        self,
        method: str,
        path: str,
        body: Any = _MISSING,
        *,
        authenticated: bool = True,
        login: bool = False,
    ) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Version": "1.0",
            "X-Gizwits-Application-Id": APP_ID,
        }
        if authenticated:
            if not self._token:
                raise BololoAuthError("A session is required")
            headers["Authorization"] = self._token
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=20),
            "allow_redirects": False,
        }
        if body is not _MISSING:
            kwargs["json"] = {"appKey": APP_ID, "data": body, "version": "1.0"}
        try:
            async with self._session.request(method, BASE_URL + path, **kwargs) as response:
                if response.status in (401, 403):
                    raise BololoAuthError("Cloud authentication was rejected")
                if response.status >= 500:
                    raise BololoConnectionError(f"Cloud returned HTTP {response.status}")
                if response.status != 200:
                    raise BololoError(f"Unexpected cloud HTTP status {response.status}")
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise BololoConnectionError("Unable to communicate with the Bololo cloud") from err
        except (ValueError, UnicodeError) as err:
            raise BololoError("Cloud returned invalid JSON") from err
        if not isinstance(payload, dict):
            raise BololoError("Cloud returned an invalid response")
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = 0
        if code != 200:
            invalid_auth = code in _AUTH_CODES or (login and code in _LOGIN_AUTH_CODES)
            error = BololoAuthError if invalid_auth else BololoError
            # Do not include server messages, which may echo user data.
            raise error(f"Cloud request was rejected (code {code})")
        return payload.get("data")
