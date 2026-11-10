"""Set up BOLOLO from the Home Assistant interface."""

from __future__ import annotations

import time
from ipaddress import IPv4Address

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BololoAuthError,
    BololoCaptchaRequired,
    BololoClient,
    BololoError,
    BololoRateLimitError,
)
from .bottle import BottleOptionsMixin
from .captcha import async_captcha_images
from .const import (
    CONF_CONNECTION_MODE,
    CONF_COUNTRY_CODE,
    CONF_DEVICES,
    CONF_LAN_HOSTS,
    CONF_LAN_PASSCODES,
    CONNECTION_MODES,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
)
from .inventory import minimal_inventory
from .schema import device_id


def _mode_selector():
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(CONNECTION_MODES),
            translation_key="connection_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class BololoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BololoOptionsFlow()

    def __init__(self):
        super().__init__()
        self._account = {}
        self._client = None
        self._captcha = None
        self._sent_at = float("-inf")
        self._authenticated = False
        self._captcha_image_id = None
        self._captcha_url = None

    def _defaults(self):
        if self.source == config_entries.SOURCE_REAUTH:
            return self._get_reauth_entry().data
        return {}

    async def async_step_user(self, user_input=None):
        errors = {}
        defaults = self._defaults()
        if user_input is not None:
            self._account = {
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_COUNTRY_CODE: user_input.get(CONF_COUNTRY_CODE, "86").strip().lstrip("+"),
                CONF_CONNECTION_MODE: user_input.get(
                    CONF_CONNECTION_MODE,
                    defaults.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE),
                ),
                "mobile_country_code": user_input.get("mobile_country_code", "CN"),
                "auth_method": user_input.get(
                    "auth_method", "password" if CONF_PASSWORD in user_input else "sms"
                ),
            }
            if not self._account[CONF_USERNAME] or not self._account[CONF_COUNTRY_CODE].isdigit():
                errors["base"] = "invalid_account"
            elif self._account["auth_method"] == "password":
                return await self.async_step_password(
                    user_input if CONF_PASSWORD in user_input else None
                )
            elif not self._account[CONF_USERNAME].isdigit():
                errors["base"] = "invalid_account"
            else:
                self._client = BololoClient(
                    async_get_clientsession(self.hass),
                    username=self._account[CONF_USERNAME],
                    country_code=self._account[CONF_COUNTRY_CODE],
                    mobile_country_code=self._account["mobile_country_code"],
                )
                return await self._send_sms()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                    vol.Required(
                        CONF_COUNTRY_CODE, default=defaults.get(CONF_COUNTRY_CODE, "86")
                    ): str,
                    vol.Required(
                        "mobile_country_code", default=defaults.get("mobile_country_code", "CN")
                    ): selector.CountrySelector(),
                    vol.Required("auth_method", default="sms"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["sms", "password"],
                            translation_key="auth_method",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_CONNECTION_MODE,
                        default=defaults.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE),
                    ): _mode_selector(),
                }
            ),
            errors=errors,
        )

    async def _finish(self):
        devices = await self._client.async_get_devices()
        data = {
            **self._account,
            "auth": self._client.auth,
            CONF_DEVICES: minimal_inventory(devices),
        }
        await self.async_set_unique_id(
            self._client.uid or f"{data[CONF_COUNTRY_CODE]}:{data[CONF_USERNAME].casefold()}"
        )
        if self.source == config_entries.SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            self._abort_if_unique_id_mismatch()
            updated = {**entry.data, **data}
            if data["auth_method"] == "sms":
                updated.pop(CONF_PASSWORD, None)
            return self.async_update_reload_and_abort(entry, data=updated)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_USERNAME], data=data)

    async def async_step_password(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._account[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            self._client = BololoClient(
                async_get_clientsession(self.hass),
                username=self._account[CONF_USERNAME],
                password=self._account[CONF_PASSWORD],
                country_code=self._account[CONF_COUNTRY_CODE],
                mobile_country_code=self._account["mobile_country_code"],
            )
            try:
                await self._client.async_login()
                return await self._finish()
            except BololoAuthError:
                errors["base"] = "invalid_auth"
            except BololoError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def _send_sms(self, captcha_code=None):
        if time.monotonic() - self._sent_at < 60:
            return await self.async_step_sms(_errors={"base": "sms_cooldown"})
        # Count attempts even on timeouts: the server may already have sent the code.
        self._sent_at = time.monotonic()
        try:
            await self._client.async_request_sms(
                self._captcha["id"] if self._captcha else None, captcha_code
            )
        except BololoCaptchaRequired as err:
            self._sent_at = float("-inf")
            try:
                self._captcha = await self._client.async_get_captcha()
                images = async_captcha_images(self.hass)
                if self._captcha_image_id is not None:
                    images.remove(self._captcha_image_id)
                self._captcha_image_id, self._captcha_url = images.add(self._captcha["image"])
            except (BololoError, ValueError):
                return await self.async_step_sms(_errors={"base": "cannot_connect"})
            error = {1000018: "invalid_captcha", 612: "expired_captcha"}.get(err.code)
            return await self.async_step_captcha(_errors={"base": error} if error else None)
        except BololoRateLimitError:
            return await self.async_step_sms(_errors={"base": "sms_rate_limit"})
        except BololoError:
            return await self.async_step_sms(_errors={"base": "cannot_connect"})
        self._clear_captcha()
        self._authenticated = False
        return await self.async_step_sms()

    def _clear_captcha(self):
        if self._captcha_image_id is not None:
            async_captcha_images(self.hass).remove(self._captcha_image_id)
        self._captcha_image_id = None
        self._captcha_url = None
        self._captcha = None

    async def async_step_captcha(self, user_input=None, *, _errors=None):
        if self._captcha is None:
            return await self.async_step_user()
        errors = _errors or {}
        if user_input is not None:
            code = user_input["captcha_code"].strip()
            if code:
                return await self._send_sms(code)
            errors["base"] = "captcha_required"
        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required("captcha_code"): str}),
            description_placeholders={"captcha_image": self._captcha_url},
            errors=errors,
        )

    async def async_step_sms(self, user_input=None, *, _errors=None):
        errors = _errors or {}
        if self._client is None:
            return await self.async_step_user()
        if user_input is not None:
            code = user_input.get("code", "").strip()
            # HA may keep the checkbox checked when the same form is shown again.
            # A supplied code must be verified, never invalidated by another send.
            if user_input.get("resend") and not code:
                return await self._send_sms()
            if not code and not self._authenticated:
                errors["base"] = "code_required"
            else:
                try:
                    if not self._authenticated:
                        await self._client.async_login_sms(code)
                        self._authenticated = True
                    return await self._finish()
                except BololoAuthError:
                    self._authenticated = False
                    errors["base"] = "invalid_code"
                except BololoError:
                    errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema(
                {vol.Optional("code"): str, vol.Optional("resend", default=False): bool}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_user()


class BololoOptionsFlow(BottleOptionsMixin, config_entries.OptionsFlow):
    """Choose routing and optionally override one known device's LAN address."""

    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None:
            devices = [state.device for state in (coordinator.data or {}).values()]
        else:
            devices = minimal_inventory(entry.data.get(CONF_DEVICES))
        self._devices = {device_id(device): device for device in devices}
        mode = entry.options.get(
            CONF_CONNECTION_MODE, entry.data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
        )
        if user_input is not None:
            if user_input.get("add_bottle"):
                return await self.async_step_add_bottle()
            self._options = dict(entry.options)
            self._options[CONF_CONNECTION_MODE] = user_input[CONF_CONNECTION_MODE]
            self._selected = user_input.get("device")
            if self._selected in self._devices:
                return await self.async_step_device()
            return self.async_create_entry(title="", data=self._options)
        fields = {vol.Required(CONF_CONNECTION_MODE, default=mode): _mode_selector()}
        fields[vol.Optional("add_bottle", default=False)] = bool
        if self._devices:
            fields[vol.Optional("device")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {
                            "value": identifier,
                            "label": str(device.get("name") or device.get("productKey")),
                        }
                        for identifier, device in self._devices.items()
                    ]
                )
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))

    async def async_step_device(self, user_input=None):
        errors = {}
        hosts = dict(self._options.get(CONF_LAN_HOSTS, {}))
        passcodes = dict(self._options.get(CONF_LAN_PASSCODES, {}))
        if user_input is not None:
            host = user_input.get("host", "").strip()
            passcode = user_input.get("passcode", "").strip()
            if host:
                try:
                    IPv4Address(host)
                except ValueError:
                    errors["host"] = "invalid_host"
            if not errors:
                for values, value in ((hosts, host), (passcodes, passcode)):
                    if value:
                        values[self._selected] = value
                    else:
                        values.pop(self._selected, None)
                self._options[CONF_LAN_HOSTS] = hosts
                self._options[CONF_LAN_PASSCODES] = passcodes
                return self.async_create_entry(title="", data=self._options)
        device = self._devices[self._selected]
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Optional("host", default=hosts.get(self._selected, "")): str,
                    vol.Optional(
                        "passcode", default=passcodes.get(self._selected, "")
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={
                "device": str(device.get("name") or device.get("productKey"))
            },
            errors=errors,
        )
