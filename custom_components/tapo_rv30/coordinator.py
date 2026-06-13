"""DataUpdateCoordinator for Tapo RV30."""
from __future__ import annotations

import base64
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from PIL import Image, ImageDraw, ImageFont

from .const import (
    DOMAIN,
    FEATURE_INTERVAL,
    FAST_INTERVAL,
    MAP_INTERVAL,
    ROOM_PALETTE,
    WALL_COLOR,
    UNKNOWN_COLOR,
    FLOOR_COLOR,
)
from .tpap import SETTING_DEFINITIONS, TapoVacuumClient

_LOGGER = logging.getLogger(__name__)

MAP_SCALE = 4   # px per vacuum grid cell → ~700×700 output image


def _lz4_block_decompress(data: bytes, uncompressed_size: int) -> bytes:
    """Pure-Python LZ4 block decompressor — no C extension needed."""
    out = bytearray(uncompressed_size)
    src = 0
    dst = 0
    n = len(data)
    while src < n:
        token = data[src]; src += 1
        # Literal run
        lit_len = token >> 4
        if lit_len == 15:
            while src < n:
                extra = data[src]; src += 1
                lit_len += extra
                if extra != 255:
                    break
        out[dst:dst + lit_len] = data[src:src + lit_len]
        src += lit_len
        dst += lit_len
        if src >= n:
            break
        # Match copy
        offset = data[src] | (data[src + 1] << 8); src += 2
        match_len = (token & 0xF) + 4
        if match_len == 19:  # 4 + 15
            while src < n:
                extra = data[src]; src += 1
                match_len += extra
                if extra != 255:
                    break
        match_pos = dst - offset
        for i in range(match_len):
            out[dst + i] = out[match_pos + i]
        dst += match_len
    return bytes(out)
FONT_SIZE  = 14


def _b64name(s: str) -> str:
    try:
        return base64.b64decode(s).decode(errors="replace").strip()
    except Exception:
        return s


def _render_map_image(map_data: dict) -> bytes:
    """Decode LZ4 pixel data and produce a JPEG image as bytes."""
    width   = map_data["width"]
    height  = map_data["height"]
    pix_len = map_data["pix_len"]

    raw     = base64.b64decode(map_data["map_data"])
    pixels  = _lz4_block_decompress(raw, uncompressed_size=pix_len)

    rooms = [a for a in map_data.get("area_list", []) if a.get("type") == "room"]
    sorted_ids  = sorted(r["id"] for r in rooms)
    room_colors = {rid: ROOM_PALETTE[i % len(ROOM_PALETTE)]
                   for i, rid in enumerate(sorted_ids)}

    # Build colour lookup table (0-255)
    lut: list[tuple[int, int, int]] = [UNKNOWN_COLOR] * 256
    lut[0]   = WALL_COLOR
    lut[127] = UNKNOWN_COLOR
    lut[255] = FLOOR_COLOR
    for rid, color in room_colors.items():
        if 0 <= rid <= 255:
            lut[rid] = color

    img = Image.new("RGB", (width * MAP_SCALE, height * MAP_SCALE))
    draw = ImageDraw.Draw(img)

    # Draw pixels — rows bottom→top, cols left→right
    for row in range(height - 1, -1, -1):
        for col in range(width):
            pv    = pixels[row * width + col]
            color = lut[pv] if pv < 256 else UNKNOWN_COLOR
            screen_row = (height - 1 - row) * MAP_SCALE
            screen_col = col * MAP_SCALE
            draw.rectangle(
                [screen_col, screen_row,
                 screen_col + MAP_SCALE - 1, screen_row + MAP_SCALE - 1],
                fill=color,
            )

    # Room name labels centred in each room
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                  FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()

    for room in rooms:
        rid = room["id"]
        if rid not in room_colors:
            continue
        name = _b64name(room.get("name", ""))
        # Find centroid of all pixels belonging to this room
        xs, ys = [], []
        for row in range(height):
            for col in range(width):
                if pixels[row * width + col] == rid:
                    xs.append(col)
                    ys.append(row)
        if not xs:
            continue
        cx = int(sum(xs) / len(xs)) * MAP_SCALE + MAP_SCALE // 2
        cy = int((height - 1 - (sum(ys) / len(ys)))) * MAP_SCALE + MAP_SCALE // 2

        # Shadow + white label
        draw.text((cx + 1, cy + 1), name, fill=(0, 0, 0, 180), font=font, anchor="mm")
        draw.text((cx, cy),         name, fill=(255, 255, 255), font=font, anchor="mm")

    # Charger and vacuum markers
    charge = map_data.get("charge_coor")
    vac    = map_data.get("vac_coor")

    def _dot(gx, gy, color, radius=6):
        sx = gx * MAP_SCALE + MAP_SCALE // 2
        sy = (height - 1 - gy) * MAP_SCALE + MAP_SCALE // 2
        draw.ellipse([sx - radius, sy - radius, sx + radius, sy + radius],
                     fill=color, outline=(255, 255, 255), width=2)

    if charge:
        _dot(charge[0], charge[1], (255, 200, 0))   # amber = dock
    if vac:
        _dot(vac[0], vac[1], (0, 180, 255))          # cyan = vacuum

    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class TapoCoordinator(DataUpdateCoordinator):
    """Polls Jarvis for status + periodically re-renders map."""

    def __init__(self, hass: HomeAssistant, client: TapoVacuumClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=FAST_INTERVAL),
        )
        self.client = client
        self._map_tick = 0  # counts update cycles; refresh map every N
        self._map_cycles = MAP_INTERVAL // FAST_INTERVAL
        self._feature_tick = 0
        self._feature_cycles = FEATURE_INTERVAL // FAST_INTERVAL
        self.map_image_bytes: bytes | None = None
        self.rooms: list[dict] = []  # current rooms (area_list, type==room)
        self.map_id: int | None = None  # current map_id
        self.device_current_map_id: int | None = None
        self.available_maps: list[dict[str, Any]] = []
        self.selected_map_id: int | None = None
        self.device_name: str = "Tapo Robot Vacuum"
        self.device_model: str = "Tapo Robot Vacuum"
        self.device_info_raw: dict[str, Any] = {}
        self.component_list: list[dict[str, Any]] = []
        self.task_api: str | None = None
        self.tasks: list[dict[str, Any]] = []
        self.selected_task_id: int | None = None
        self.supported_settings: dict[str, dict[str, Any]] = {}
        self._device_bootstrapped = False

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._device_bootstrapped:
            try:
                await self.hass.async_add_executor_job(self._bootstrap_device_context)
            except Exception:
                pass

        try:
            data = await self.hass.async_add_executor_job(self.client.get_status)
        except Exception as exc:
            raise UpdateFailed(f"Failed to fetch vacuum status: {exc}") from exc

        try:
            data["consumables"] = await self.hass.async_add_executor_job(
                self.client.get_consumables
            )
        except Exception as exc:
            _LOGGER.debug("Consumables fetch failed: %s", exc)
            data["consumables"] = {}

        # Refresh map on first load and every MAP_INTERVAL seconds
        self._map_tick += 1
        self._feature_tick += 1

        if self._feature_tick >= self._feature_cycles:
            self._feature_tick = 0
            try:
                await self.hass.async_add_executor_job(self._refresh_model_state)
            except Exception as exc:
                _LOGGER.debug("Model state refresh failed: %s", exc)

        if self.map_image_bytes is None or self._map_tick >= self._map_cycles:
            self._map_tick = 0
            try:
                await self.hass.async_add_executor_job(self._refresh_map)
            except Exception as exc:
                _LOGGER.warning("Map refresh failed: %s", exc)

        return data

    def _refresh_map(self) -> None:
        current_id, map_list = self.client.get_map_info()
        self.device_current_map_id = current_id
        self.available_maps = [
            {
                "id": map_info["map_id"],
                "name": _b64name(map_info.get("map_name", ""))
                or f"Map {map_info['map_id']}",
            }
            for map_info in map_list
        ]
        available_map_ids = {map_info["id"] for map_info in self.available_maps}
        if self.selected_map_id not in available_map_ids:
            self.selected_map_id = None

        target_map_id = self.selected_map_id or current_id
        map_data = self.client.get_map_data(target_map_id)
        self.map_id = target_map_id
        self.rooms = [
            a for a in map_data.get("area_list", []) if a.get("type") == "room"
        ]
        self.map_image_bytes = _render_map_image(map_data)
        _LOGGER.debug(
            "Map rendered: %d bytes, %d rooms",
            len(self.map_image_bytes),
            len(self.rooms),
        )

    def _bootstrap_device_context(self) -> None:
        info = self.client.get_device_info()
        self.device_info_raw = info
        self.device_name = _b64name(info.get("nickname", "")) or info.get(
            "model", "Tapo Robot Vacuum"
        )
        self.device_model = info.get("model", "Tapo Robot Vacuum")
        self.component_list = self.client.get_component_list()
        self._refresh_model_state()
        self._device_bootstrapped = True

    def _refresh_model_state(self) -> None:
        self.task_api, self.tasks = self.client.list_tasks()
        if self.selected_task_id is not None and not any(
            task["id"] == self.selected_task_id for task in self.tasks
        ):
            self.selected_task_id = None
        self.supported_settings = self.client.get_supported_settings()

    async def async_refresh_model_state(self) -> None:
        await self.hass.async_add_executor_job(self._refresh_model_state)

    def resolve_task_live(
        self, task_id: int | None = None, task_name: str | None = None
    ) -> dict[str, Any]:
        task_api, tasks = self.client.list_tasks()
        self.task_api = task_api
        self.tasks = tasks

        if task_id is not None:
            for task in tasks:
                if task["id"] == task_id:
                    return task
            raise ValueError(f"Task id '{task_id}' not found")

        if task_name is not None:
            exact = [
                task for task in tasks if task["name"].lower() == task_name.lower()
            ]
            matches = exact or [
                task for task in tasks if task_name.lower() in task["name"].lower()
            ]
            if not matches:
                available = [task["name"] for task in tasks]
                raise ValueError(
                    f"Task '{task_name}' not found. Available: {available}"
                )
            return matches[0]

        raise ValueError("Either task_id or task_name is required")

    def get_task_options(self) -> list[str]:
        """Return task names for the task select entity."""
        return [task["name"] for task in self.tasks]

    def get_map_options(self) -> list[str]:
        """Return map names for the map select entity."""
        return [map_info["name"] for map_info in self.available_maps]

    def get_selected_map_name(self) -> str | None:
        """Return the currently selected map name for the integration."""
        if self.map_id is None:
            return None
        for map_info in self.available_maps:
            if map_info["id"] == self.map_id:
                return map_info["name"]
        return None

    def select_map_live(self, map_name: str) -> None:
        """Select a stored map by name and refresh the rendered map context."""
        for map_info in self.available_maps:
            if map_info["name"] == map_name:
                self.selected_map_id = map_info["id"]
                self.client.set_current_map(map_info["id"])
                self._refresh_map()
                return
        raise ValueError(f"Map '{map_name}' not found")

    def get_selected_task_name(self) -> str | None:
        """Return the last selected task name if it still exists."""
        if self.selected_task_id is None:
            return None
        for task in self.tasks:
            if task["id"] == self.selected_task_id:
                return task["name"]
        return None

    def get_setting_field_value(self, setting_key: str) -> Any:
        value = self.supported_settings.get(setting_key)
        if value is None:
            return None

        definition = SETTING_DEFINITIONS.get(setting_key, {})
        field = definition.get("field")
        if field:
            return value.get(field)
        return value

    def resolve_rooms_live(
        self, name_patterns: list[str], map_name: str | None = None
    ) -> tuple[list[int], int]:
        """Fetch rooms live from device, resolve names → (room_ids, map_id).

        Uses map_name (partial match) if given, otherwise current map.
        Raises ValueError if map or any room is not found.
        """
        current_map_id, map_list = self.client.get_map_info()

        if map_name:
            target_id = next(
                (
                    m["map_id"]
                    for m in map_list
                    if map_name.lower() in _b64name(m.get("map_name", "")).lower()
                ),
                None,
            )
            if target_id is None:
                available = [_b64name(m.get("map_name", "")) for m in map_list]
                raise ValueError(f"Map '{map_name}' not found. Available: {available}")
        else:
            target_id = self.selected_map_id or current_map_id

        map_data = self.client.get_map_data(target_id)
        rooms = [a for a in map_data.get("area_list", []) if a.get("type") == "room"]

        matched: list[int] = []
        seen: set[int] = set()
        for pat in name_patterns:
            decoded = [_b64name(r.get("name", "")) for r in rooms]
            exact = [r for r, n in zip(rooms, decoded) if n.lower() == pat.lower()]
            hits = exact or [
                r for r, n in zip(rooms, decoded) if pat.lower() in n.lower()
            ]
            if not hits:
                available = [_b64name(r.get("name", "")) for r in rooms]
                raise ValueError(f"No room matching '{pat}'. Available: {available}")
            for r in hits:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    matched.append(r["id"])

        return matched, target_id
