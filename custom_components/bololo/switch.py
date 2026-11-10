"""Writable boolean datapoints."""

from homeassistant.components.switch import SwitchEntity

from .entity import BololoEntity, async_setup_datapoints


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "switch", BololoSwitch)


class BololoSwitch(BololoEntity, SwitchEntity):
    @property
    def is_on(self):
        return self.value

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_write(self.identifier, self.point, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_write(self.identifier, self.point, False)
