"""Switch entities for supported Tapo robot boolean settings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BOOL_SETTING_ENTITIES, DOMAIN
from .coordinator import TapoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TapoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TapoSettingSwitch(coordinator, entry, setting_key, meta)
        for setting_key, meta in BOOL_SETTING_ENTITIES.items()
        if setting_key in coordinator.supported_settings
    ]
    async_add_entities(entities)


class TapoSettingSwitch(CoordinatorEntity[TapoCoordinator], SwitchEntity):
    """A boolean robot setting exposed as a switch entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TapoCoordinator,
        entry: ConfigEntry,
        setting_key: str,
        meta: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._setting_key = setting_key
        self._attr_name = meta["name"]
        self._attr_icon = meta["icon"]
        self._attr_unique_id = f"{entry.entry_id}_{setting_key}_switch"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self.coordinator.device_name,
            "manufacturer": "TP-Link",
            "model": self.coordinator.device_model,
        }

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.get_setting_field_value(self._setting_key)
        if value is None:
            return None
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.set_named_setting, self._setting_key, True
        )
        await self.coordinator.async_refresh_model_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.client.set_named_setting, self._setting_key, False
        )
        await self.coordinator.async_refresh_model_state()
        await self.coordinator.async_request_refresh()