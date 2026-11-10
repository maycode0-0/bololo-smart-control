"""Writable enum datapoints using the original wire order."""

from homeassistant.components.select import SelectEntity

from .entity import BololoEntity, async_setup_datapoints


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "select", BololoSelect)


class BololoSelect(BololoEntity, SelectEntity):
    def __init__(self, coordinator, identifier, point):
        super().__init__(coordinator, identifier, point)
        self._attr_options = list(point.labels or point.options)

    @property
    def current_option(self):
        return self.value

    async def async_select_option(self, option):
        await self.coordinator.async_write(self.identifier, self.point, option)
