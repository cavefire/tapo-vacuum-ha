"""Button entities for dock actions supported by the Tapo robot vacuum."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TapoCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[TapoDockActionButton] = []

    action_specs = [
        (
            "dust_collection",
            "empty_bin",
            "Empty Dust Bin",
            "mdi:delete-empty",
            "start_dust_collection",
        ),
        (
            "back_wash_mode",
            "wash_mop",
            "Wash Mop",
            "mdi:water-sync",
            "start_wash_mop",
        ),
        (
            "dry_mop_mode",
            "dry_mop",
            "Dry Mop",
            "mdi:tumble-dryer",
            "start_dry_mop",
        ),
        (
            "cut_hair_mode",
            "cut_hair",
            "Remove Hair",
            "mdi:content-cut",
            "start_cut_hair",
        ),
    ]

    for setting_key, unique_suffix, name, icon, action in action_specs:
        if setting_key not in coordinator.supported_settings:
            continue

        entities.append(
            TapoDockActionButton(
                coordinator,
                entry,
                unique_suffix=unique_suffix,
                name=name,
                icon=icon,
                action=action,
            )
        )

    async_add_entities(entities)


class TapoDockActionButton(CoordinatorEntity[TapoCoordinator], ButtonEntity):
    """A dock action exposed as a button."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TapoCoordinator,
        entry: ConfigEntry,
        *,
        unique_suffix: str,
        name: str,
        icon: str,
        action: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self.coordinator.device_name,
            "manufacturer": "TP-Link",
            "model": self.coordinator.device_model,
        }

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            getattr(self.coordinator.client, self._action)
        )
        await self.coordinator.async_request_refresh()