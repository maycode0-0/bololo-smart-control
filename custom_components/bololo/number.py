"""Writable numeric datapoints in the app's engineering units."""

from homeassistant.components.number import NumberEntity, NumberMode

from .entity import BololoEntity, async_setup_datapoints
from .model_overrides import P30, Z60, Z60_BOUNDS


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "number", BololoNumber)


class BololoNumber(BololoEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, identifier, point):
        super().__init__(coordinator, identifier, point)
        self._attr_native_min_value = point.minimum
        self._attr_native_max_value = point.maximum
        self._attr_native_step = point.step
        if point.key in {"temp_set", "tea_set_temp", "appointment_temp"}:
            self._attr_native_unit_of_measurement = "°C"
        product_key = coordinator.data[identifier].product.key
        if product_key == P30 and point.key == "wateryield":
            self._attr_native_unit_of_measurement = "mL"
        if product_key == Z60 and point.key in Z60_BOUNDS:
            self._attr_native_unit_of_measurement = "min"

    @property
    def native_value(self):
        return self.value

    async def async_set_native_value(self, value):
        await self.coordinator.async_write(self.identifier, self.point, value)
