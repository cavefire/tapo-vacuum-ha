"""Config flow for Tapo RV30."""
from __future__ import annotations

import base64
from copy import deepcopy
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import DEFAULT_PORT, DOMAIN
from .tpap import TASK_API_CUSTOM, TASK_API_QUICK, AuthError, TapoVacuumClient

STEP_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_USERNAME, default=""): str,
    vol.Required(CONF_PASSWORD, default=""): str,
})

CONF_TASK_ID = "task_id"
CONF_TASK_NAME = "task_name"
CONF_TASK_PAYLOAD = "task_payload"
CONF_TASK_MAP_ID = "task_map_id"
CONF_TASK_ROOM_IDS = "task_room_ids"
CONF_TASK_GROUP_ICON = "task_group_icon"
CONF_TASK_SUCTION = "task_suction"
CONF_TASK_CISTERN = "task_cistern"
CONF_TASK_CLEAN_NUMBER = "task_clean_number"
CONF_TASK_DENSITY = "task_density"
CONF_TASK_CLEAN_ORDER = "task_clean_order"
CONF_TASK_ROOM_ORDER = "task_room_order"

SUCTION_LABELS = {
    1: "Off",
    2: "Quiet",
    3: "Standard",
    4: "Turbo",
    5: "Max",
}
WATER_LABELS = {0: "None", 1: "Low", 2: "Moderate", 3: "High"}
PASSES_LABELS = {1: "1", 2: "2", 3: "3"}
DENSITY_LABELS = {1: "Fast", 2: "Standard", 3: "Deep"}
ICON_LABELS = {1: "Icon 1", 2: "Icon 2", 3: "Icon 3"}


def _decode_name(value: str) -> str:
    if not value:
        return value
    try:
        return base64.b64decode(value).decode(errors="replace").strip()
    except Exception:
        return value


def _task_id_key(task_api: str) -> str:
    return "group_id" if task_api == TASK_API_QUICK else "rule_id"


def _task_name_key(task_api: str) -> str:
    return "group_name" if task_api == TASK_API_QUICK else "rule_name"


def _prepare_task_payload(task_api: str, detail: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(detail)
    payload.pop(_task_id_key(task_api), None)
    payload.pop(_task_name_key(task_api), None)
    return payload


def _build_default_task_payload(
    task_api: str, tasks: list[dict[str, Any]], current_map_id: int
) -> dict[str, Any]:
    if tasks:
        return _prepare_task_payload(task_api, tasks[0]["detail"])

    if task_api == TASK_API_QUICK:
        return {
            "group_icon": 1,
            "map_id": current_map_id,
            "recommend_task_id": 0,
            "is_default": False,
            "invalid": 0,
            "group_info": [],
        }

    return {
        "mode": "room",
        "map_id": current_map_id,
        "clean_order": True,
        "area_list": [],
        "invalid": 0,
    }


def _quick_task_defaults(detail: dict[str, Any] | None) -> dict[str, Any]:
    group = ((detail or {}).get("group_info") or [{}])[0]
    return {
        CONF_TASK_GROUP_ICON: (detail or {}).get("group_icon", 1),
        CONF_TASK_SUCTION: group.get("suction", 3),
        CONF_TASK_CISTERN: group.get("cistern", 2),
        CONF_TASK_CLEAN_NUMBER: group.get("clean_number", 1),
        CONF_TASK_DENSITY: group.get("density", 3),
    }


def _custom_rule_defaults(detail: dict[str, Any] | None) -> dict[str, Any]:
    area = ((detail or {}).get("area_list") or [{}])[0]
    return {
        CONF_TASK_SUCTION: area.get("suction", 4),
        CONF_TASK_CISTERN: area.get("cistern", 1),
        CONF_TASK_CLEAN_NUMBER: area.get("clean_number", 1),
        CONF_TASK_CLEAN_ORDER: (detail or {}).get("clean_order", True),
    }


def _select_options(labels: dict[int, str], *, max_value: int | None = None) -> list[selector.SelectOptionDict]:
    return [
        selector.SelectOptionDict(value=str(value), label=label)
        for value, label in labels.items()
        if max_value is None or value <= max_value
    ]


async def _test_connection(hass: HomeAssistant, host: str, user: str, pw: str) -> str | None:
    """Return None on success, error key string on failure."""
    def _try():
        c = TapoVacuumClient(host, user, pw, DEFAULT_PORT)
        c.authenticate()
    try:
        await hass.async_add_executor_job(_try)
        return None
    except AuthError:
        return "invalid_auth"
    except Exception:
        return "cannot_connect"


class TapoRV30ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._task_api: str | None = None
        self._tasks: list[dict[str, Any]] = []
        self._current_map_id: int | None = None
        self._selected_task: dict[str, Any] | None = None
        self._map_context: list[dict[str, Any]] = []
        self._selected_map_id: int | None = None
        self._guided_task_name: str = ""
        self._guided_room_ids: list[int] = []
        self._guided_group_icon: int = 1
        self._guided_clean_order: bool = True

    def _build_client(self, entry: ConfigEntry) -> TapoVacuumClient:
        return TapoVacuumClient(
            entry.data[CONF_HOST],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            DEFAULT_PORT,
        )

    async def _async_load_task_context(self, entry: ConfigEntry) -> None:
        def _load() -> tuple[str | None, list[dict[str, Any]], int, list[dict[str, Any]]]:
            client = self._build_client(entry)
            task_api, tasks = client.list_tasks()
            current_map_id, _ = client.get_map_info()
            _, map_list = client.get_map_info()
            map_context = []
            for map_info in map_list:
                map_data = client.get_map_data(map_info["map_id"])
                rooms = []
                for room in map_data.get("area_list", []):
                    if room.get("type") != "room":
                        continue
                    rooms.append(
                        {
                            "id": room["id"],
                            "name": _decode_name(room.get("name", "")),
                            "encoded_name": room.get("name", ""),
                            "type": room.get("type"),
                        }
                    )
                map_context.append(
                    {
                        "map_id": map_info["map_id"],
                        "map_name": _decode_name(map_info.get("map_name", "")),
                        "raw": map_info,
                        "rooms": rooms,
                    }
                )
            return task_api, tasks, current_map_id, map_context

        (
            self._task_api,
            self._tasks,
            self._current_map_id,
            self._map_context,
        ) = await self.hass.async_add_executor_job(_load)

    def _task_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(
                value=str(task["id"]),
                label=f"{task['name']} (ID {task['id']})",
            )
            for task in self._tasks
        ]

    def _map_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(
                value=str(map_info["map_id"]),
                label=map_info["map_name"] or f"Map {map_info['map_id']}",
            )
            for map_info in self._map_context
        ]

    def _rooms_for_selected_map(self) -> list[dict[str, Any]]:
        if self._selected_map_id is None:
            return []
        for map_info in self._map_context:
            if map_info["map_id"] == self._selected_map_id:
                return map_info["rooms"]
        return []

    def _room_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(
                value=str(room["id"]),
                label=room["name"] or f"Room {room['id']}",
            )
            for room in self._rooms_for_selected_map()
        ]

    def _selected_room_names(self, room_ids: list[int]) -> dict[int, str]:
        rooms = {room["id"]: room for room in self._rooms_for_selected_map()}
        return {room_id: rooms[room_id]["encoded_name"] for room_id in room_ids if room_id in rooms}

    def _find_selected_task(self, task_id: str) -> dict[str, Any]:
        for task in self._tasks:
            if str(task["id"]) == task_id:
                return task
        raise ValueError(f"Task id '{task_id}' not found")

    def _map_id_for_task(self, task: dict[str, Any]) -> int:
        return int(task.get("map_id") or self._current_map_id or 0)

    def _room_ids_for_task(self, task: dict[str, Any]) -> list[int]:
        detail = task["detail"]
        if task["api"] == TASK_API_QUICK:
            group = (detail.get("group_info") or [{}])[0]
            room_ids = group.get("room_list") or [area["id"] for area in group.get("area_list", [])]
            return list(room_ids)
        return [area["id"] for area in detail.get("area_list", []) if "id" in area]

    def _guided_default_room_ids(self) -> list[int]:
        if self._selected_task is None or self._selected_map_id is None:
            return []
        if self._selected_map_id != self._map_id_for_task(self._selected_task):
            return []
        return self._room_ids_for_task(self._selected_task)

    def _room_name(self, room_id: int) -> str:
        rooms = {room["id"]: room for room in self._rooms_for_selected_map()}
        room = rooms.get(room_id)
        if room is None:
            return f"Room {room_id}"
        return room["name"] or f"Room {room_id}"

    def _room_config_defaults(
        self,
        room_ids: list[int],
        default_values: dict[str, Any],
        detail: dict[str, Any] | None,
    ) -> dict[int, dict[str, int]]:
        room_defaults = {
            room_id: {
                CONF_TASK_SUCTION: int(default_values[CONF_TASK_SUCTION]),
                CONF_TASK_CISTERN: int(default_values[CONF_TASK_CISTERN]),
                CONF_TASK_CLEAN_NUMBER: int(default_values[CONF_TASK_CLEAN_NUMBER]),
                CONF_TASK_DENSITY: int(default_values.get(CONF_TASK_DENSITY, 3)),
            }
            for room_id in room_ids
        }

        if not detail:
            return room_defaults

        if self._task_api == TASK_API_QUICK:
            group = (detail.get("group_info") or [{}])[0]
            area_list = group.get("area_list") or []
            if area_list:
                for area in area_list:
                    room_id = area.get("id")
                    if room_id not in room_defaults:
                        continue
                    room_defaults[room_id] = {
                        CONF_TASK_SUCTION: int(area.get("suction", room_defaults[room_id][CONF_TASK_SUCTION])),
                        CONF_TASK_CISTERN: int(area.get("cistern", room_defaults[room_id][CONF_TASK_CISTERN])),
                        CONF_TASK_CLEAN_NUMBER: int(area.get("clean_number", room_defaults[room_id][CONF_TASK_CLEAN_NUMBER])),
                        CONF_TASK_DENSITY: int(area.get("density", room_defaults[room_id][CONF_TASK_DENSITY])),
                    }
            return room_defaults

        for area in detail.get("area_list", []):
            room_id = area.get("id")
            if room_id not in room_defaults:
                continue
            room_defaults[room_id] = {
                CONF_TASK_SUCTION: int(area.get("suction", room_defaults[room_id][CONF_TASK_SUCTION])),
                CONF_TASK_CISTERN: int(area.get("cistern", room_defaults[room_id][CONF_TASK_CISTERN])),
                CONF_TASK_CLEAN_NUMBER: int(area.get("clean_number", room_defaults[room_id][CONF_TASK_CLEAN_NUMBER])),
                CONF_TASK_DENSITY: int(room_defaults[room_id][CONF_TASK_DENSITY]),
            }

        return room_defaults

    def _room_field_key(self, room_id: int, field: str) -> str:
        return f"room_{room_id}_{field}"

    def _task_form_schema(
        self, *, default_name: str, default_payload: dict[str, Any]
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_TASK_NAME, default=default_name): selector.TextSelector(),
                vol.Required(CONF_TASK_PAYLOAD, default=default_payload): selector.ObjectSelector(),
            }
        )

    def _guided_task_schema(
        self,
        *,
        default_name: str,
        default_room_ids: list[int],
        default_values: dict[str, Any],
    ) -> vol.Schema:
        schema: dict[Any, Any] = {
            vol.Required(CONF_TASK_NAME, default=default_name): selector.TextSelector(),
            vol.Required(CONF_TASK_ROOM_IDS, default=[str(room_id) for room_id in default_room_ids]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._room_options(), multiple=True)
            ),
        }

        if self._task_api == TASK_API_QUICK:
            schema[vol.Required(CONF_TASK_GROUP_ICON, default=str(default_values[CONF_TASK_GROUP_ICON]))] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=_select_options(ICON_LABELS))
            )
        else:
            schema[vol.Required(CONF_TASK_CLEAN_ORDER, default=default_values[CONF_TASK_CLEAN_ORDER])] = selector.BooleanSelector()

        return vol.Schema(schema)

    def _guided_room_schema(
        self,
        *,
        room_ids: list[int],
        default_room_configs: dict[int, dict[str, int]],
    ) -> vol.Schema:
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_TASK_ROOM_ORDER,
                default=[str(room_id) for room_id in room_ids],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=str(room_id), label=self._room_name(room_id)
                        )
                        for room_id in room_ids
                    ],
                    multiple=True,
                )
            )
        }

        for room_id in room_ids:
            room_name = self._room_name(room_id)
            room_defaults = default_room_configs[room_id]
            schema[vol.Required(
                self._room_field_key(room_id, CONF_TASK_SUCTION),
                default=str(room_defaults[CONF_TASK_SUCTION]),
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_select_options(
                        SUCTION_LABELS,
                        max_value=5 if self._task_api == TASK_API_QUICK else 4,
                    )
                )
            )
            schema[vol.Required(
                self._room_field_key(room_id, CONF_TASK_CISTERN),
                default=str(room_defaults[CONF_TASK_CISTERN]),
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=_select_options(WATER_LABELS))
            )
            schema[vol.Required(
                self._room_field_key(room_id, CONF_TASK_CLEAN_NUMBER),
                default=str(room_defaults[CONF_TASK_CLEAN_NUMBER]),
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=_select_options(PASSES_LABELS))
            )
            if self._task_api == TASK_API_QUICK:
                schema[vol.Required(
                    self._room_field_key(room_id, CONF_TASK_DENSITY),
                    default=str(room_defaults[CONF_TASK_DENSITY]),
                )] = selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_select_options(DENSITY_LABELS))
                )

        return vol.Schema(schema)

    def _build_guided_payload(self, user_input: dict[str, Any]) -> dict[str, Any]:
        room_order = [int(room_id) for room_id in user_input[CONF_TASK_ROOM_ORDER]]
        if not room_order:
            raise ValueError("At least one room must be selected")

        if self._selected_map_id is None:
            raise ValueError("A map must be selected")

        if self._task_api == TASK_API_QUICK:
            area_list = [
                {
                    "id": room_id,
                    "suction": int(user_input[self._room_field_key(room_id, CONF_TASK_SUCTION)]),
                    "cistern": int(user_input[self._room_field_key(room_id, CONF_TASK_CISTERN)]),
                    "clean_number": int(user_input[self._room_field_key(room_id, CONF_TASK_CLEAN_NUMBER)]),
                    "density": int(user_input[self._room_field_key(room_id, CONF_TASK_DENSITY)]),
                    "type": "room",
                    "clean_type": 2,
                }
                for room_id in room_order
            ]
            return {
                "group_name": self._guided_task_name,
                "group_icon": self._guided_group_icon,
                "map_id": self._selected_map_id,
                "recommend_task_id": 0,
                "is_default": False,
                "invalid": 0,
                "group_info": [
                    {
                        "clean_mode": 3,
                        "is_custom": True,
                        "area_list": area_list,
                        "room_list": room_order,
                        "invalid": 0,
                    }
                ],
            }

        room_names = self._selected_room_names(room_order)
        return {
            "rule_name": self._guided_task_name,
            "mode": "room",
            "map_id": self._selected_map_id,
            "clean_order": self._guided_clean_order,
            "area_list": [
                {
                    "vertexs": [],
                    "id": room_id,
                    "cistern": int(user_input[self._room_field_key(room_id, CONF_TASK_CISTERN)]),
                    "suction": int(user_input[self._room_field_key(room_id, CONF_TASK_SUCTION)]),
                    "tag": "",
                    "clean_number": int(user_input[self._room_field_key(room_id, CONF_TASK_CLEAN_NUMBER)]),
                    "type": "room",
                    "name": room_names.get(room_id, ""),
                }
                for room_id in room_order
            ],
            "invalid": 0,
        }

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            user = user_input[CONF_USERNAME].strip()
            pw   = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            err = await _test_connection(self.hass, host, user, pw)
            if err:
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title=f"Tapo RV30 ({host})",
                    data={CONF_HOST: host, CONF_USERNAME: user, CONF_PASSWORD: pw},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "connection",
                "task_add_payload",
                "task_add_guided_map",
                "task_edit_payload_select",
                "task_edit_guided_select",
                "task_delete",
            ],
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            user = user_input[CONF_USERNAME].strip()
            pw = user_input[CONF_PASSWORD]
            err = await _test_connection(self.hass, host, user, pw)
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: host,
                        CONF_USERNAME: user,
                        CONF_PASSWORD: pw,
                    },
                )

        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(STEP_SCHEMA, entry.data),
            errors=errors,
        )

    async def async_step_task_add_payload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._task_api is None:
            try:
                await self._async_load_task_context(entry)
            except Exception:
                errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(
                step_id="task_add_payload",
                data_schema=self._task_form_schema(default_name="", default_payload={}),
                errors=errors,
            )

        if self._task_api is None or self._current_map_id is None:
            return self.async_abort(reason="unsupported_tasks")

        if user_input is not None:
            payload = dict(user_input[CONF_TASK_PAYLOAD])
            payload[_task_name_key(self._task_api)] = user_input[CONF_TASK_NAME]
            if self._task_api == TASK_API_CUSTOM and "map_id" not in payload:
                payload["map_id"] = self._current_map_id

            try:
                await self.hass.async_add_executor_job(
                    partial(
                        self._build_client(entry).upsert_task,
                        payload,
                        task_api=self._task_api,
                    )
                )
            except Exception:
                errors["base"] = "cannot_save_task"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})

        return self.async_show_form(
            step_id="task_add_payload",
            data_schema=self._task_form_schema(
                default_name="",
                default_payload=_build_default_task_payload(
                    self._task_api,
                    self._tasks,
                    self._current_map_id,
                ),
            ),
            errors=errors,
        )

    async def async_step_task_add_guided_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()

        try:
            await self._async_load_task_context(entry)
        except Exception:
            return self.async_abort(reason="cannot_connect")

        if self._task_api is None:
            return self.async_abort(reason="unsupported_tasks")

        if user_input is not None:
            self._selected_map_id = int(user_input[CONF_TASK_MAP_ID])
            return await self.async_step_task_add_guided_details()

        return self.async_show_form(
            step_id="task_add_guided_map",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TASK_MAP_ID,
                        default=str(self._current_map_id),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._map_options())
                    )
                }
            ),
        )

    async def async_step_task_add_guided_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._selected_map_id is None or self._task_api is None:
            return await self.async_step_task_add_guided_map()

        default_values = (
            _quick_task_defaults(self._tasks[0]["detail"] if self._task_api == TASK_API_QUICK and self._tasks else None)
            if self._task_api == TASK_API_QUICK
            else _custom_rule_defaults(self._tasks[0]["detail"] if self._tasks else None)
        )

        if user_input is not None:
            room_ids = [int(room_id) for room_id in user_input[CONF_TASK_ROOM_IDS]]
            if not room_ids:
                errors["base"] = "invalid_guided_task"
            else:
                self._guided_task_name = user_input[CONF_TASK_NAME]
                self._guided_room_ids = room_ids
                if self._task_api == TASK_API_QUICK:
                    self._guided_group_icon = int(user_input[CONF_TASK_GROUP_ICON])
                else:
                    self._guided_clean_order = bool(user_input[CONF_TASK_CLEAN_ORDER])
                return await self.async_step_task_add_guided_rooms()

        return self.async_show_form(
            step_id="task_add_guided_details",
            data_schema=self._guided_task_schema(
                default_name="",
                default_room_ids=[],
                default_values=default_values,
            ),
            errors=errors,
        )

    async def async_step_task_add_guided_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._selected_map_id is None or self._task_api is None or not self._guided_room_ids:
            return await self.async_step_task_add_guided_details()

        default_values = (
            _quick_task_defaults(self._tasks[0]["detail"] if self._task_api == TASK_API_QUICK and self._tasks else None)
            if self._task_api == TASK_API_QUICK
            else _custom_rule_defaults(self._tasks[0]["detail"] if self._tasks else None)
        )
        default_room_configs = self._room_config_defaults(self._guided_room_ids, default_values, None)

        if user_input is not None:
            try:
                payload = self._build_guided_payload(user_input)
                await self.hass.async_add_executor_job(
                    partial(
                        self._build_client(entry).upsert_task,
                        payload,
                        task_api=self._task_api,
                    )
                )
            except ValueError:
                errors["base"] = "invalid_guided_task"
            except Exception:
                errors["base"] = "cannot_save_task"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})

        return self.async_show_form(
            step_id="task_add_guided_rooms",
            data_schema=self._guided_room_schema(
                room_ids=self._guided_room_ids,
                default_room_configs=default_room_configs,
            ),
            errors=errors,
        )

    async def async_step_task_edit_payload_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        try:
            await self._async_load_task_context(entry)
        except Exception:
            return self.async_abort(reason="cannot_connect")

        if self._task_api is None:
            return self.async_abort(reason="unsupported_tasks")

        if not self._tasks:
            return self.async_abort(reason="no_tasks")

        if user_input is not None:
            self._selected_task = self._find_selected_task(user_input[CONF_TASK_ID])
            return await self.async_step_task_edit_payload()

        return self.async_show_form(
            step_id="task_edit_payload_select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TASK_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._task_options())
                    )
                }
            ),
        )

    async def async_step_task_edit_payload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._selected_task is None or self._task_api is None:
            return await self.async_step_task_edit_payload_select()

        if user_input is not None:
            payload = dict(user_input[CONF_TASK_PAYLOAD])
            payload[_task_id_key(self._task_api)] = self._selected_task["id"]
            payload[_task_name_key(self._task_api)] = user_input[CONF_TASK_NAME]
            if self._task_api == TASK_API_CUSTOM and "map_id" not in payload:
                payload["map_id"] = self._selected_task["map_id"]

            try:
                await self.hass.async_add_executor_job(
                    partial(
                        self._build_client(entry).upsert_task,
                        payload,
                        task_api=self._task_api,
                    )
                )
            except Exception:
                errors["base"] = "cannot_save_task"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})

        return self.async_show_form(
            step_id="task_edit_payload",
            data_schema=self._task_form_schema(
                default_name=self._selected_task["name"],
                default_payload=_prepare_task_payload(
                    self._task_api,
                    self._selected_task["detail"],
                ),
            ),
            errors=errors,
            description_placeholders={"task_name": self._selected_task["name"]},
        )

    async def async_step_task_edit_guided_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        try:
            await self._async_load_task_context(entry)
        except Exception:
            return self.async_abort(reason="cannot_connect")

        if self._task_api is None:
            return self.async_abort(reason="unsupported_tasks")

        if not self._tasks:
            return self.async_abort(reason="no_tasks")

        if user_input is not None:
            self._selected_task = self._find_selected_task(user_input[CONF_TASK_ID])
            self._selected_map_id = self._map_id_for_task(self._selected_task)
            return await self.async_step_task_edit_guided_map()

        return self.async_show_form(
            step_id="task_edit_guided_select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TASK_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._task_options())
                    )
                }
            ),
        )

    async def async_step_task_edit_guided_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._selected_task is None:
            return await self.async_step_task_edit_guided_select()

        if user_input is not None:
            self._selected_map_id = int(user_input[CONF_TASK_MAP_ID])
            return await self.async_step_task_edit_guided_details()

        return self.async_show_form(
            step_id="task_edit_guided_map",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TASK_MAP_ID,
                        default=str(self._selected_map_id),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._map_options())
                    )
                }
            ),
            description_placeholders={"task_name": self._selected_task["name"]},
        )

    async def async_step_task_edit_guided_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._selected_task is None or self._task_api is None:
            return await self.async_step_task_edit_guided_select()

        detail = self._selected_task["detail"]
        default_values = (
            _quick_task_defaults(detail)
            if self._task_api == TASK_API_QUICK
            else _custom_rule_defaults(detail)
        )

        if user_input is not None:
            try:
                room_ids = [int(room_id) for room_id in user_input[CONF_TASK_ROOM_IDS]]
            except (TypeError, ValueError):
                room_ids = []

            if not room_ids:
                errors["base"] = "invalid_guided_task"
            else:
                self._guided_task_name = user_input[CONF_TASK_NAME]
                self._guided_room_ids = room_ids
                if self._task_api == TASK_API_QUICK:
                    self._guided_group_icon = int(user_input[CONF_TASK_GROUP_ICON])
                else:
                    self._guided_clean_order = bool(user_input[CONF_TASK_CLEAN_ORDER])
                return await self.async_step_task_edit_guided_rooms()

        return self.async_show_form(
            step_id="task_edit_guided_details",
            data_schema=self._guided_task_schema(
                default_name=self._selected_task["name"],
                default_room_ids=self._guided_default_room_ids(),
                default_values=default_values,
            ),
            errors=errors,
            description_placeholders={"task_name": self._selected_task["name"]},
        )

    async def async_step_task_edit_guided_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if self._selected_task is None or self._task_api is None or not self._guided_room_ids:
            return await self.async_step_task_edit_guided_details()

        detail = self._selected_task["detail"]
        default_values = (
            _quick_task_defaults(detail)
            if self._task_api == TASK_API_QUICK
            else _custom_rule_defaults(detail)
        )
        default_room_configs = self._room_config_defaults(
            self._guided_room_ids,
            default_values,
            detail,
        )

        if user_input is not None:
            try:
                payload = self._build_guided_payload(user_input)
                payload[_task_id_key(self._task_api)] = self._selected_task["id"]
                await self.hass.async_add_executor_job(
                    partial(
                        self._build_client(entry).upsert_task,
                        payload,
                        task_api=self._task_api,
                    )
                )
            except ValueError:
                errors["base"] = "invalid_guided_task"
            except Exception:
                errors["base"] = "cannot_save_task"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})

        return self.async_show_form(
            step_id="task_edit_guided_rooms",
            data_schema=self._guided_room_schema(
                room_ids=self._guided_room_ids,
                default_room_configs=default_room_configs,
            ),
            errors=errors,
            description_placeholders={"task_name": self._selected_task["name"]},
        )

    async def async_step_task_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        try:
            await self._async_load_task_context(entry)
        except Exception:
            return self.async_abort(reason="cannot_connect")

        if self._task_api is None:
            return self.async_abort(reason="unsupported_tasks")

        if not self._tasks:
            return self.async_abort(reason="no_tasks")

        if user_input is not None:
            task = self._find_selected_task(user_input[CONF_TASK_ID])
            try:
                await self.hass.async_add_executor_job(
                    partial(
                        self._build_client(entry).delete_task,
                        task["id"],
                        map_id=task.get("map_id"),
                        task_api=task.get("api"),
                    )
                )
            except Exception:
                errors["base"] = "cannot_delete_task"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})

        return self.async_show_form(
            step_id="task_delete",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TASK_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._task_options())
                    )
                }
            ),
            errors=errors,
        )
