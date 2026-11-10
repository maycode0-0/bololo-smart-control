"""Read-only numeric and enumerated device datapoints."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .const import DOMAIN
from .entity import BololoEntity, async_setup_datapoints
from .history import history_kinds, history_name
from .schema import Datapoint


async def async_setup_entry(hass, entry, async_add_entities):
    async_setup_datapoints(hass, entry, async_add_entities, "sensor", BololoSensor)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known = set()

    def discover():
        entities = []
        for identifier, state in (coordinator.data or {}).items():
            kinds = list(history_kinds(state.product))
            if "water" in kinds:
                kinds.append("cloud_water")
            for kind in kinds:
                key = (identifier, kind)
                if key not in known:
                    known.add(key)
                    entities.append(BololoHistorySensor(coordinator, identifier, kind))
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


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


class BololoHistorySensor(BololoEntity, SensorEntity):
    """Count observed operations and expose a bounded recent journal."""

    _attr_icon = "mdi:clipboard-text-clock-outline"
    _attr_native_unit_of_measurement = "次"

    def __init__(self, coordinator, identifier, kind):
        self.kind = kind
        name = "官方出水历史" if kind == "cloud_water" else history_name(kind)
        super().__init__(
            coordinator, identifier, Datapoint(kind + "_history", name, "uint32", False)
        )

    @property
    def journal(self):
        if self.kind == "cloud_water":
            manager = self.coordinator.cloud_history
            return manager.summary(self.identifier) if manager else {}
        return self.coordinator.history.summary(self.identifier, self.kind)

    @property
    def value(self):
        journal = self.journal
        return journal.get("synced_count", 0) if self.kind == "cloud_water" else journal["count"]

    @property
    def native_value(self):
        return self.value

    @property
    def available(self):
        # Saved history remains readable while the appliance/cloud is offline.
        return self.identifier in (self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        journal = self.journal
        return {
            "record_source": "cloud" if self.kind == "cloud_water" else "home_assistant",
            **journal,
        }
