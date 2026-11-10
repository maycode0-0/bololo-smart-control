"""Read-only boolean datapoints."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import BololoEntity, async_setup_datapoints


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "binary_sensor", BololoBinarySensor)


class BololoBinarySensor(BololoEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.value
