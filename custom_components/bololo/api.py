"""Async client for the Bololo AEP API recovered from the Android app.

The app's modules 1161, 1221, 1222 and 1271 document the wire format.
In particular, control requests wrap a JSON *string* in the AEP envelope.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

BASE_URL = "https://app.bololoapp3.com/"
APP_ID = "91732ca20dfd4517b164c38aac162c43"
_MISSING = object()
_AUTH_CODES = {505, 526}
_LOGIN_AUTH_CODES = {504, 1000000, 1000033, 1000034, 1000035}


class BololoError(Exception):
    """An invalid or unsuccessful cloud response."""


class BololoAuthError(BololoError):
    """Credentials or a session were rejected."""


class BololoCaptchaRequired(BololoError):
    """The SMS endpoint requires a human-entered image challenge."""


class BololoRateLimitError(BololoError):
    """The server declined another SMS request."""


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
        *,
        mobile_country_code: str = "CN",
        auth: dict[str, Any] | None = None,
        on_auth_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._country_code = country_code
        self._mobile_country_code = mobile_country_code
        self._token = token
        self._uid: str | None = None
        self._refresh_token: str | None = None
        self._created_at: float | None = None
        self._expired_at: float | None = None
        self._auth_lock = asyncio.Lock()
        self._on_auth_update = on_auth_update
        if isinstance(auth, dict):
            self._token = auth.get("token") or None
            self._refresh_token = auth.get("refresh_token") or None
            self._uid = str(auth["uid"]) if auth.get("uid") is not None else None
            self._created_at = self._timestamp(auth.get("created_at"))
            self._expired_at = self._timestamp(auth.get("expired_at"))

    @property
    def auth(self) -> dict[str, Any]:
        """Copy of the session suitable for private Home Assistant storage."""
        return {
            "token": self._token,
            "refresh_token": self._refresh_token,
            "created_at": self._created_at,
            "expired_at": self._expired_at,
            "uid": self._uid,
        }

    async def async_request_sms(
        self, captcha_id: str | None = None, captcha_code: str | None = None
    ) -> None:
        """Send one login code only after an explicit user request."""
        headers = {}
        if captcha_id and captcha_code:
            headers = {
                "X-Gizwits-Captcha-Id": captcha_id,
                "X-Gizwits-Captcha-Code": captcha_code,
            }
        code_token = await self._raw_request("GET", "app/user/codeToken", authenticated=False)
        body = {"mobile": "+" + self._country_code.lstrip("+") + self._username, "lang": "zh"}
        if code_token:
            body["token"] = code_token
        await self._raw_request(
            "POST",
            "app/user/v2/mobileCode" if code_token else "app/user/mobileCode",
            body,
            authenticated=False,
            extra_headers=headers,
        )

    async def async_get_captcha(self) -> dict[str, str]:
        """Retrieve, but never solve, the SMS image challenge."""
        data = await self._raw_request("GET", "app/captcha/gen", authenticated=False)
        if not isinstance(data, dict):
            raise BololoError("Invalid captcha response")
        identifier = data.get("captchaId") or data.get("id")
        picture = data.get("captchaImage") or data.get("image") or data.get("imageBase64")
        if not isinstance(identifier, str) or not isinstance(picture, str) or not picture:
            raise BololoError("Invalid captcha response")
        if not picture.startswith("data:image/"):
            picture = "data:image/jpeg;base64," + picture
        # Restrict server content interpolated into the Markdown image to base64 images.
        if not re.fullmatch(
            r"data:image/(?:jpeg|png|gif|webp);base64,[A-Za-z0-9+/=\r\n]+", picture
        ):
            raise BololoError("Invalid captcha image")
        try:
            prefix, encoded = picture.split(",", 1)
            encoded = "".join(encoded.split())
            base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as err:
            raise BololoError("Invalid captcha image") from err
        return {"id": identifier, "image": prefix + "," + encoded}

    async def async_login_sms(self, code: str) -> None:
        """Exchange an OTP for a session without retaining the OTP."""
        async with self._auth_lock:
            data = await self._raw_request(
                "POST",
                "app/user/loginByMobile",
                {
                    "mobile": "+" + self._country_code.lstrip("+") + self._username,
                    "code": code,
                    "homeName": "我的家",
                    "mobileCountryCode": self._mobile_country_code,
                    "lang": "zh",
                },
                authenticated=False,
                login=True,
            )
            await self._accept_login(data)

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
        await self._accept_login(data)

    async def _accept_login(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise BololoError("Invalid login response")
        authentication = data.get("jwtAuthenticationDto")
        token = authentication.get("token") if isinstance(authentication, dict) else None
        if not isinstance(token, str) or not token:
            raise BololoError("Login response does not contain a token")
        await self._update_auth(
            token,
            data.get("refreshToken"),
            data.get("createdAt"),
            data.get("expiredAt"),
            data.get("uid"),
        )

    async def _update_auth(
        self, token: str, refresh_token: Any, created_at: Any, expired_at: Any, uid: Any
    ) -> None:
        next_uid = str(uid) if uid is not None else self._uid
        if self._uid is not None and next_uid != self._uid:
            raise BololoAuthError("Session belongs to another account")
        candidate = {
            "token": token,
            "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
            "created_at": self._timestamp(created_at),
            "expired_at": self._timestamp(expired_at),
            "uid": next_uid,
        }
        # Persist before exposing a rotated token to other requests.
        if self._on_auth_update is not None:
            await self._on_auth_update(dict(candidate))
        self._token = candidate["token"]
        self._refresh_token = candidate["refresh_token"]
        self._created_at = candidate["created_at"]
        self._expired_at = candidate["expired_at"]
        self._uid = next_uid

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
                await self._update_auth(
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
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "Content-Type": "application/json",
            "Version": "1.0",
            "X-Gizwits-Application-Id": APP_ID,
        }
        if extra_headers:
            headers.update(extra_headers)
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
            if code in {610, 612, 1000018}:
                raise BololoCaptchaRequired("An image verification is required")
            if (
                code in {53, 8000005}
                or payload.get("message") == "yunpian send sms response error: code = 8"
            ):
                raise BololoRateLimitError("SMS requests are rate limited")
            invalid_auth = code in _AUTH_CODES or (login and code in _LOGIN_AUTH_CODES)
            error = BololoAuthError if invalid_auth else BololoError
            # Do not include server messages, which may echo user data.
            raise error(f"Cloud request was rejected (code {code})")
        return payload.get("data")
