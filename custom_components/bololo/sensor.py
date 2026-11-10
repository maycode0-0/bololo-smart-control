"""Read-only numeric and enumerated device datapoints."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .entity import BololoEntity, async_setup_datapoints


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "sensor", BololoSensor)


class BololoSensor(BololoEntity, SensorEntity):
    def __init__(self, coordinator, identifier, point):
        super().__init__(coordinator, identifier, point)
        if point.kind == "enum":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(point.labels or point.options)
        elif point.key == "temp_current":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = "°C"
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return self.value
