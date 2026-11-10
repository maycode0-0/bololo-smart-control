"""Set up BOLOLO from the Home Assistant interface."""

from __future__ import annotations

from ipaddress import IPv4Address

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BololoAuthError, BololoClient, BololoError
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

    async def async_step_user(self, user_input=None):
        errors = {}
        default_mode = DEFAULT_CONNECTION_MODE
        if self.source == config_entries.SOURCE_REAUTH:
            default_mode = self._get_reauth_entry().data.get(
                CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE
            )
        if user_input is not None:
            user_input = dict(user_input)
            user_input.setdefault(CONF_CONNECTION_MODE, default_mode)
            user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
            user_input[CONF_COUNTRY_CODE] = user_input[CONF_COUNTRY_CODE].strip().lstrip("+")
            client = BololoClient(
                async_get_clientsession(self.hass),
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                country_code=user_input[CONF_COUNTRY_CODE],
            )
            try:
                await client.async_login()
                devices = await client.async_get_devices()
            except BololoAuthError:
                errors["base"] = "invalid_auth"
            except BololoError:
                errors["base"] = "cannot_connect"
            else:
                user_input[CONF_DEVICES] = minimal_inventory(devices)
                await self.async_set_unique_id(
                    client.uid
                    or f"{user_input[CONF_COUNTRY_CODE]}:{user_input[CONF_USERNAME].casefold()}"
                )
                if self.source == config_entries.SOURCE_REAUTH:
                    entry = self._get_reauth_entry()
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(entry, data_updates=user_input)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_USERNAME], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_COUNTRY_CODE, default="86"): str,
                    vol.Required(CONF_CONNECTION_MODE, default=default_mode): _mode_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_user()


class BololoOptionsFlow(config_entries.OptionsFlow):
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
            self._options = dict(entry.options)
            self._options[CONF_CONNECTION_MODE] = user_input[CONF_CONNECTION_MODE]
            self._selected = user_input.get("device")
            if self._selected in self._devices:
                return await self.async_step_device()
            return self.async_create_entry(title="", data=self._options)
        fields = {vol.Required(CONF_CONNECTION_MODE, default=mode): _mode_selector()}
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
