"""Tapo RV30 Robot Vacuum integration."""
from __future__ import annotations

import logging
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    DEFAULT_PORT,
    DOMAIN,
    SERVICE_CLEAN_ROOMS,
    SERVICE_DELETE_TASK,
    SERVICE_REORDER_TASKS,
    SERVICE_SAVE_TASK,
    SERVICE_SET_ROBOT_SETTING,
    SERVICE_START_TASK,
)
from .coordinator import TapoCoordinator
from .tpap import TASK_API_CUSTOM, TASK_API_QUICK, TapoVacuumClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VACUUM,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = TapoVacuumClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        port=DEFAULT_PORT,
    )
    coordinator = TapoCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def _resolve_coordinator(call: ServiceCall) -> TapoCoordinator:
        entity_ids: list[str] = call.data.get("entity_id", [])
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state and state.attributes.get("integration") == DOMAIN:
                return next(iter(hass.data[DOMAIN].values()))
        return next(iter(hass.data[DOMAIN].values()))

    async def handle_clean_rooms(call: ServiceCall) -> None:
        """Service: tapo_rv30.clean_rooms."""
        coord = _resolve_coordinator(call)
        rooms_raw = call.data.get("rooms", [])
        map_name: str | None = call.data.get("map")

        if isinstance(rooms_raw, str):
            rooms = [rooms_raw]
        else:
            rooms = list(rooms_raw)

        if not rooms:
            _LOGGER.error("Clean rooms: 'rooms' field is required")
            return

        try:
            room_ids, map_id = await hass.async_add_executor_job(
                coord.resolve_rooms_live, rooms, map_name
            )
            await hass.async_add_executor_job(
                coord.client.clean_rooms, room_ids, map_id
            )
            await coord.async_request_refresh()
        except ValueError as exc:
            _LOGGER.error("Clean rooms: %s", exc)

    async def handle_start_task(call: ServiceCall) -> None:
        """Service: tapo_rv30.start_task."""
        coord = _resolve_coordinator(call)
        task = await hass.async_add_executor_job(
            coord.resolve_task_live,
            call.data.get("task_id"),
            call.data.get("task_name"),
        )
        await hass.async_add_executor_job(
            partial(
                coord.client.start_task,
                task["id"],
                map_id=task.get("map_id"),
                task_api=task.get("api"),
            )
        )
        coord.selected_task_id = task["id"]
        await coord.async_request_refresh()

    async def handle_save_task(call: ServiceCall) -> None:
        """Service: tapo_rv30.save_task."""
        coord = _resolve_coordinator(call)
        task_api = (
            call.data.get("task_api")
            or coord.task_api
            or coord.client.detect_task_api()
        )
        payload = dict(call.data.get("task") or {})

        if "name" in payload:
            if task_api == TASK_API_QUICK:
                payload["group_name"] = payload.pop("name")
            elif task_api == TASK_API_CUSTOM:
                payload["rule_name"] = payload.pop("name")
        if "task_id" in payload:
            if task_api == TASK_API_QUICK:
                payload["group_id"] = payload.pop("task_id")
            elif task_api == TASK_API_CUSTOM:
                payload["rule_id"] = payload.pop("task_id")
        if task_api == TASK_API_CUSTOM and "map_id" not in payload:
            payload["map_id"] = coord.map_id

        await hass.async_add_executor_job(
            partial(coord.client.upsert_task, payload, task_api=task_api)
        )
        await coord.async_refresh_model_state()
        await coord.async_request_refresh()

    async def handle_delete_task(call: ServiceCall) -> None:
        """Service: tapo_rv30.delete_task."""
        coord = _resolve_coordinator(call)
        task = await hass.async_add_executor_job(
            coord.resolve_task_live,
            call.data.get("task_id"),
            call.data.get("task_name"),
        )
        await hass.async_add_executor_job(
            partial(
                coord.client.delete_task,
                task["id"],
                map_id=task.get("map_id"),
                task_api=task.get("api"),
            )
        )
        await coord.async_refresh_model_state()
        await coord.async_request_refresh()

    async def handle_reorder_tasks(call: ServiceCall) -> None:
        """Service: tapo_rv30.reorder_tasks."""
        coord = _resolve_coordinator(call)
        task_ids = list(call.data.get("task_ids", []))
        await hass.async_add_executor_job(
            partial(coord.client.reorder_tasks, task_ids, task_api=coord.task_api)
        )
        await coord.async_refresh_model_state()
        await coord.async_request_refresh()

    async def handle_set_robot_setting(call: ServiceCall) -> None:
        """Service: tapo_rv30.set_robot_setting."""
        coord = _resolve_coordinator(call)
        setting = call.data["setting"]
        value = call.data.get("value")
        await hass.async_add_executor_job(
            coord.client.set_named_setting, setting, value
        )
        await coord.async_refresh_model_state()
        await coord.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_ROOMS):
        hass.services.async_register(DOMAIN, SERVICE_CLEAN_ROOMS, handle_clean_rooms)
        hass.services.async_register(DOMAIN, SERVICE_START_TASK, handle_start_task)
        hass.services.async_register(DOMAIN, SERVICE_SAVE_TASK, handle_save_task)
        hass.services.async_register(DOMAIN, SERVICE_DELETE_TASK, handle_delete_task)
        hass.services.async_register(
            DOMAIN, SERVICE_REORDER_TASKS, handle_reorder_tasks
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SET_ROBOT_SETTING, handle_set_robot_setting
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_CLEAN_ROOMS)
            hass.services.async_remove(DOMAIN, SERVICE_START_TASK)
            hass.services.async_remove(DOMAIN, SERVICE_SAVE_TASK)
            hass.services.async_remove(DOMAIN, SERVICE_DELETE_TASK)
            hass.services.async_remove(DOMAIN, SERVICE_REORDER_TASKS)
            hass.services.async_remove(DOMAIN, SERVICE_SET_ROBOT_SETTING)
    return ok
