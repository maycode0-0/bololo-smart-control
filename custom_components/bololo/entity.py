"""Shared datapoint entity discovery and availability."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BololoCoordinator
from .schema import Datapoint


def async_setup_datapoints(
    hass, entry, async_add_entities, platform: str, entity_class: Callable
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known = set()

    def discover():
        entities = []
        for identifier, state in (coordinator.data or {}).items():
            points = state.product.datapoints
            if platform == "sensor":
                points += (
                    Datapoint("status_time", "上次状态上报", "string", False, internal=True),
                )
            for point in points:
                key = (identifier, point.key)
                if point.platform == platform and key not in known:
                    known.add(key)
                    entities.append(entity_class(coordinator, identifier, point))
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class BololoEntity(CoordinatorEntity[BololoCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BololoCoordinator, identifier: str, point: Datapoint) -> None:
        super().__init__(coordinator)
        self.identifier = identifier
        self.point = point
        self._attr_unique_id = f"{identifier}_{point.key}"
        self._attr_name = point.name
        self._attr_entity_registry_enabled_default = not point.internal
        if point.internal:
            self._attr_entity_category = (
                EntityCategory.DIAGNOSTIC if not point.writable else EntityCategory.CONFIG
            )
        state = coordinator.data[identifier]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=state.device.get("name") or state.product.name,
            manufacturer="BOLOLO 波咯咯",
            model=state.product.name,
        )

    @property
    def value(self):
        state = (self.coordinator.data or {}).get(self.identifier)
        return self.point.decode(state.attrs.get(self.point.key)) if state else None

    @property
    def available(self) -> bool:
        state = (self.coordinator.data or {}).get(self.identifier)
        return super().available and bool(state and state.available and self.value is not None)

    @property
    def extra_state_attributes(self):
        state = (self.coordinator.data or {}).get(self.identifier)
        if not state:
            return None
        return {
            "connection_mode": getattr(self.coordinator.client, "mode", "cloud"),
            "active_transport": state.transport,
            "cloud_status_time": state.attrs.get("status_time")
            if state.transport == "cloud"
            else None,
        }
