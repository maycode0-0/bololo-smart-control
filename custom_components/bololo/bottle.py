"""Explicit, in-person P30 bottle registration through the options flow."""

from __future__ import annotations

import math

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import BololoError
from .const import DOMAIN
from .model_overrides import P30


def _choices(items):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[{"value": key, "label": label} for key, label in items]
        )
    )


class BottleOptionsMixin:
    """Guide the user without guessing weights or replaying physical commands."""

    def _bottle_finish(self):
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    def _bottle_form(self, step, errors=None, **placeholders):
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Optional("confirm", default=False): bool,
                    vol.Optional("cancel", default=False): bool,
                }
            ),
            errors=errors or {},
            description_placeholders=placeholders,
        )

    async def _bottle_cancel(self):
        try:
            await self._bottle_coordinator.async_bottle_command(
                self._bottle_identifier, {"bottle_tag": 0, "new_bottles": 0}
            )
        except HomeAssistantError:
            return self.async_abort(reason="bottle_reset_failed")
        return self._bottle_finish()

    async def _bottle_command(self, command):
        try:
            await self._bottle_coordinator.async_bottle_command(self._bottle_identifier, command)
        except HomeAssistantError:
            # The device may have accepted the command: never replay on submit.
            return self.async_abort(reason="bottle_command_uncertain")
        return None

    async def async_step_add_bottle(self, user_input=None):
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="bottle_integration_unavailable")
        if coordinator.client.mode == "lan" or coordinator.client.cloud is None:
            return self.async_abort(reason="bottle_cloud_required")
        devices = {
            identifier: state
            for identifier, state in (coordinator.data or {}).items()
            if state.product.key == P30 and state.available
        }
        if not devices:
            return self.async_abort(reason="bottle_no_device")
        errors = {}
        if user_input is not None:
            identifier = user_input.get("device")
            if identifier not in devices:
                errors["device"] = "bottle_invalid_device"
            else:
                self._bottle_coordinator = coordinator
                self._bottle_identifier = identifier
                self._bottle_device = devices[identifier].device
                try:
                    babies = await coordinator.client.cloud.async_get_babies(self._bottle_device)
                    self._bottle_babies = {
                        str(baby["id"]): baby for baby in babies if baby.get("id") is not None
                    }
                except BololoError:
                    errors["base"] = "bottle_cannot_connect"
                else:
                    if not self._bottle_babies:
                        return self.async_abort(reason="bottle_no_baby")
                    return await self.async_step_bottle_details()
        return self.async_show_form(
            step_id="add_bottle",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): _choices(
                        (key, str(state.device.get("name") or state.product.name))
                        for key, state in devices.items()
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_bottle_details(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("cancel"):
                return self._bottle_finish()
            name = user_input.get("name", "").strip()
            baby = self._bottle_babies.get(user_input.get("baby"))
            if not 1 <= len(name) <= 7:
                errors["name"] = "bottle_invalid_name"
            if baby is None:
                errors["baby"] = "bottle_invalid_baby"
            if not errors:
                self._bottle_name = name
                self._bottle_baby_id = baby["id"]
                self._bottle_save_attempted = False
                failure = await self._bottle_command({"bottle_tag": 0, "new_bottles": 0})
                return failure or await self.async_step_bottle_empty_tray()
        return self.async_show_form(
            step_id="bottle_details",
            data_schema=vol.Schema(
                {
                    vol.Required("baby"): _choices(
                        (key, str(baby.get("name") or key))
                        for key, baby in self._bottle_babies.items()
                    ),
                    vol.Required("name"): str,
                    vol.Optional("cancel", default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_bottle_empty_tray(self, user_input=None):
        if user_input is not None:
            if user_input.get("cancel"):
                return await self._bottle_cancel()
            if user_input.get("confirm"):
                failure = await self._bottle_command({"bottle_tag": 1})
                return failure or await self.async_step_bottle_place()
        return self._bottle_form("bottle_empty_tray")

    async def async_step_bottle_place(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("cancel"):
                return await self._bottle_cancel()
            if user_input.get("confirm"):
                state = (self._bottle_coordinator.data or {}).get(self._bottle_identifier)
                # A stale number from a previous session must never be saved.
                if state is None or state.attrs.get("new_bottles") not in (0, "0"):
                    errors["base"] = "bottle_reset_pending"
                    await self._bottle_coordinator.async_request_refresh()
                else:
                    failure = await self._bottle_command({"bottle_tag": 2})
                    return failure or await self.async_step_bottle_result()
        return self._bottle_form("bottle_place", errors)

    async def async_step_bottle_result(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("cancel"):
                return await self._bottle_cancel()
            if user_input.get("confirm"):
                await self._bottle_coordinator.async_request_refresh()
                state = (self._bottle_coordinator.data or {}).get(self._bottle_identifier)
                attrs = state.attrs if state and state.available else {}
                try:
                    number = float(attrs.get("new_bottles"))
                    weight = float(attrs.get("bottle_weight"))
                except (TypeError, ValueError):
                    number, weight = 0, 0
                if not self._bottle_coordinator.last_update_success:
                    errors["base"] = "bottle_cannot_connect"
                elif number == 254:
                    errors["base"] = "bottle_registration_rejected"
                elif number == 255:
                    errors["base"] = "bottle_too_light"
                elif (
                    not isinstance(attrs.get("new_bottles"), bool)
                    and not isinstance(attrs.get("bottle_weight"), bool)
                    and math.isfinite(number)
                    and number.is_integer()
                    and 1 <= number <= 253
                    and math.isfinite(weight)
                    and weight > 0
                ):
                    self._bottle_payload = {
                        "name": self._bottle_name,
                        "icon": "",
                        "babyId": self._bottle_baby_id,
                        "num": int(number),
                        "bottleWeight": weight,
                    }
                    return await self.async_step_bottle_save()
                else:
                    errors["base"] = "bottle_result_pending"
        return self._bottle_form("bottle_result", errors)

    async def async_step_bottle_save(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("cancel"):
                return await self._bottle_cancel()
            if user_input.get("confirm"):
                if self._bottle_save_attempted:
                    errors["base"] = "bottle_save_unknown"
                else:
                    self._bottle_save_attempted = True
                    try:
                        await self._bottle_coordinator.client.cloud.async_create_bottle(
                            self._bottle_payload
                        )
                    except BololoError:
                        errors["base"] = "bottle_save_unknown"
                    else:
                        failure = await self._bottle_command({"bottle_tag": 0, "new_bottles": 0})
                        return failure or self._bottle_finish()
        return self._bottle_form(
            "bottle_save", errors, name=self._bottle_name, number=str(self._bottle_payload["num"])
        )
