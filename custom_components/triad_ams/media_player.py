"""MediaPlayer platform for Triad AMS outputs."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant, ServiceValidationError, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import State
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import TriadCoordinator

from .const import DOMAIN
from .models import TriadAmsInput, TriadAmsOutput

_LOGGER = logging.getLogger(__name__)


def _build_input_names(
    hass: HomeAssistant,
    active_inputs: list[int],
    input_links_opt: dict[str, str],
    *,
    state_getter: Callable[[HomeAssistant, str], State | None] | None = None,
) -> dict[int, str]:
    """Build input names dict from linked entities or defaults."""
    if state_getter is None:

        def state_getter(h: HomeAssistant, eid: str) -> State | None:
            return h.states.get(eid)

    input_names: dict[int, str] = {}
    for i in active_inputs:
        ent_id = input_links_opt.get(str(i))
        if ent_id:
            st = state_getter(hass, ent_id)
            if st:
                input_names[i] = st.name
                continue
        input_names[i] = f"Input {i}"
    return input_names


def _build_inputs(
    active_inputs: list[int],
    input_names: dict[int, str],
    input_links_opt: dict[str, str],
) -> list[TriadAmsInput]:
    """Build input models."""
    return [
        TriadAmsInput(i, input_names.get(i, f"Input {i}"), input_links_opt.get(str(i)))
        for i in sorted(active_inputs)
    ]


def _build_outputs(
    active_outputs: list[int],
    coordinator: TriadCoordinator,
    input_names: dict[int, str],
) -> list[TriadAmsOutput]:
    """Build output models."""
    outputs: list[TriadAmsOutput] = []
    for ch in sorted(active_outputs):
        outputs.append(
            TriadAmsOutput(ch, f"Output {ch}", coordinator, outputs, input_names)
        )
    return outputs


def _create_input_entities(
    inputs: list[TriadAmsInput], entry: ConfigEntry
) -> dict[int, TriadAmsSourceMediaPlayer]:
    """Create input entities."""
    return {
        inp.number: TriadAmsSourceMediaPlayer(inp, entry)
        for inp in inputs
        if inp.linked_entity_id
    }


def _find_linked_input(
    _output: TriadAmsOutput,
    input_links_opt: dict[str, str],
    input_entities: dict[int, TriadAmsSourceMediaPlayer],
) -> str | None:
    """Find the input entity linked to an output."""
    for inp_num, inp_entity in input_entities.items():
        if inp_entity.input.linked_entity_id == input_links_opt.get(str(inp_num)):
            return inp_entity.entity_id
    return None


def _create_output_entities(
    outputs: list[TriadAmsOutput],
    entry: ConfigEntry,
    input_links_opt: dict[str, str],
    input_entities: dict[int, TriadAmsSourceMediaPlayer],
) -> list[TriadAmsMediaPlayer]:
    """Create output entities."""
    output_entities: list[TriadAmsMediaPlayer] = []
    for output in outputs:
        linked_input_id = _find_linked_input(output, input_links_opt, input_entities)
        entity = TriadAmsMediaPlayer(output, entry, input_links_opt, linked_input_id)
        output_entities.append(entity)
    return output_entities


def _populate_group_members(
    input_entities: dict[int, TriadAmsSourceMediaPlayer],
    output_entities: list[TriadAmsMediaPlayer],
) -> None:
    """Populate group members for input entities after they're added to HA."""
    for input_entity in input_entities.values():
        members = [
            out.entity_id
            for out in output_entities
            if out.input_player_entity_id == input_entity.entity_id
        ]
        input_entity.group_members = members
        # Trigger state update to reflect new group_members
        input_entity.async_write_ha_state()


@callback
def _update_input_name_from_state(  # noqa: PLR0913
    hass: HomeAssistant,
    input_num: int,
    entity_id: str,
    input_names: dict[int, str],
    entities: list[TriadAmsMediaPlayer],
    *,
    state_getter: Callable[[HomeAssistant, str], State | None] | None = None,
) -> None:
    """Update input name from entity state and notify entities."""
    if state_getter is None:

        def state_getter(h: HomeAssistant, eid: str) -> State | None:
            return h.states.get(eid)

    new_state = state_getter(hass, entity_id)
    if new_state and new_state.name:
        old_name = input_names.get(input_num, f"Input {input_num}")
        new_name = new_state.name
        if old_name != new_name:
            _LOGGER.debug(
                "Updating input %d name from '%s' to '%s' (entity: %s)",
                input_num,
                old_name,
                new_name,
                entity_id,
            )
            input_names[input_num] = new_name
            for entity in entities:
                entity.async_write_ha_state()


@callback
def _create_input_link_handler(  # noqa: PLR0913
    hass: HomeAssistant,
    input_links_opt: dict[str, str],
    active_inputs: list[int],
    input_names: dict[int, str],
    entities: list[TriadAmsMediaPlayer],
    *,
    state_getter: Callable[[HomeAssistant, str], State | None] | None = None,
) -> Any:
    """Create callback handler for input link state changes."""

    @callback
    def _handle_input_link_state_change(event: Any) -> None:
        """Handle state changes from linked input entities."""
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return

        # Find which input number this entity is linked to
        input_num = None
        for input_str, linked_ent_id in input_links_opt.items():
            if linked_ent_id == entity_id:
                try:
                    input_num = int(input_str)
                    break
                except ValueError:
                    continue

        if input_num is None or input_num not in active_inputs:
            return

        _update_input_name_from_state(
            hass, input_num, entity_id, input_names, entities, state_getter=state_getter
        )

    return _handle_input_link_state_change


def _setup_input_link_subscriptions(  # noqa: PLR0913
    hass: HomeAssistant,
    coordinator: Any,
    input_links_opt: dict[str, str],
    active_inputs: list[int],
    input_names: dict[int, str],
    entities: list[TriadAmsMediaPlayer],
    *,
    state_getter: Callable[[HomeAssistant, str], State | None] | None = None,
) -> None:
    """Set up subscriptions to linked entity state changes."""
    linked_entity_ids = [ent_id for ent_id in input_links_opt.values() if ent_id]
    if not linked_entity_ids:
        return

    handler = _create_input_link_handler(
        hass,
        input_links_opt,
        active_inputs,
        input_names,
        entities,
        state_getter=state_getter,
    )
    unsub = async_track_state_change_event(hass, linked_entity_ids, handler)

    # Store unsubscribe function to clean up on unload
    # Accessing private member is intentional for cleanup tracking
    if not hasattr(coordinator, "_input_link_unsubs"):
        coordinator._input_link_unsubs = []  # noqa: SLF001
    coordinator._input_link_unsubs.append(unsub)  # noqa: SLF001

    # Check immediately for any entities that might have become available
    for i in active_inputs:
        ent_id = input_links_opt.get(str(i))
        if ent_id and (
            i not in input_names or input_names.get(i, "").startswith("Input ")
        ):
            _update_input_name_from_state(
                hass, i, ent_id, input_names, entities, state_getter=state_getter
            )


def _cleanup_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    inputs: list[TriadAmsInput],
    outputs: list[TriadAmsOutput],
    *,
    entity_registry_getter: Any = None,
) -> None:
    """Clean up stale and orphaned entities."""
    if entity_registry_getter is None:
        entity_registry_getter = er.async_get
    allowed = {f"{entry.entry_id}_output_{o.number}" for o in outputs}
    registry = entity_registry_getter(hass)
    for ent in list(registry.entities.values()):
        if (
            ent.platform == DOMAIN
            and ent.config_entry_id == entry.entry_id
            and ent.unique_id not in allowed
        ):
            registry.async_remove(ent.entity_id)


def _remove_orphaned_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    entity_registry_getter: Any = None,
    device_registry_getter: Any = None,
    entries_for_device_getter: Any = None,
) -> None:
    """Remove orphaned devices (those without any entities for this entry)."""
    if entity_registry_getter is None:
        entity_registry_getter = er.async_get
    if device_registry_getter is None:
        device_registry_getter = dr.async_get
    if entries_for_device_getter is None:
        entries_for_device_getter = er.async_entries_for_device
    registry = entity_registry_getter(hass)
    dev_reg = device_registry_getter(hass)
    for device in list(dev_reg.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue
        if not entries_for_device_getter(
            registry, device.id, include_disabled_entities=True
        ):
            dev_reg.async_remove_device(device.id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Triad AMS media player entities from a config entry."""
    _LOGGER.debug(
        "async_setup_entry called for Triad AMS: entry_id=%s, data=%s, options=%s",
        entry.entry_id,
        entry.data,
        entry.options,
    )
    # Use the coordinator attached by __init__.py in entry.runtime_data
    coordinator = entry.runtime_data

    # Use only the minimal active channel lists from options
    active_inputs: list[int] = entry.options.get("active_inputs", [])
    active_outputs: list[int] = entry.options.get("active_outputs", [])
    input_links_opt: dict[str, str] = entry.options.get("input_links", {})

    input_names = _build_input_names(hass, active_inputs, input_links_opt)
    inputs = _build_inputs(active_inputs, input_names, input_links_opt)
    outputs = _build_outputs(active_outputs, coordinator, input_names)

    try:
        await coordinator.start()
    except Exception:
        _LOGGER.exception("Failed to start TriadCoordinator")

    # Register outputs for lightweight rolling polling
    for output in outputs:
        coordinator.register_output(output)

    # Create input proxy entities for linked inputs
    input_entities = _create_input_entities(inputs, entry)
    # Create output entities
    output_entities = _create_output_entities(
        outputs, entry, input_links_opt, input_entities
    )

    entities: list[MediaPlayerEntity] = list(input_entities.values()) + output_entities
    async_add_entities(entities)

    _LOGGER.debug(
        "Entities added to Home Assistant: %s", [e.unique_id for e in entities]
    )

    # Populate group members after entities are added and have entity_ids
    _populate_group_members(input_entities, output_entities)

    _setup_input_link_subscriptions(
        hass, coordinator, input_links_opt, active_inputs, input_names, entities
    )
    _cleanup_stale_entities(hass, entry, inputs, outputs)
    _remove_orphaned_devices(hass, entry)


class TriadAmsMediaPlayer(MediaPlayerEntity):
    """Media player entity representing a Triad AMS output."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.GROUPING
    )
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        output: TriadAmsOutput,
        entry: ConfigEntry,
        input_links: dict[str, str | None],
        *,
        state_getter: Callable[[HomeAssistant, str], State | None] | None = None,
        input_player_entity_id: str | None = None,
    ) -> None:
        """Initialize a Triad AMS output media player entity."""
        self.output = output
        self._entry = entry
        self._input_links = input_links
        self._input_player_entity_id = input_player_entity_id
        self._linked_entity_id: str | None = None
        self._linked_unsub: callable | None = None
        self._output_unsub: callable | None = None
        self._options = entry.options
        if state_getter is not None:
            self._state_getter = state_getter
        else:

            def default_state_getter(h: HomeAssistant, eid: str) -> State | None:
                return h.states.get(eid)

            self._state_getter = default_state_getter
        # Keep per-entry unique entity IDs stable
        self._attr_unique_id = f"{entry.entry_id}_output_{output.number}"
        # Entity name part; with has_entity_name this becomes the suffix
        self._attr_name = f"Output {output.number}"
        self._attr_has_entity_name = True
        self._attr_device_class = MediaPlayerDeviceClass.SPEAKER
        # Group all outputs under one device per config entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Triad",
            "model": "Audio Matrix",
        }

    @property
    def input_player_entity_id(self) -> str | None:
        """Return the linked input media player entity ID."""
        return self._input_player_entity_id

    @property
    def group_members(self) -> list[str] | None:
        """Return this entity as a member of the linked input player group if linked."""
        if self._input_player_entity_id is None:
            return None
        return [self.entity_id]

    @property
    def group_leader(self) -> bool:
        """Return whether output is a group leader."""
        return False

    # ---- Optional linked upstream media attribute proxying ----
    def _current_linked_entity_id(self) -> str | None:
        src = self.output.source
        if src is None:
            return None
        # Options store keys as strings, but tests may inject int keys; check both
        return self._input_links.get(src) or self._input_links.get(str(src))

    @callback
    def _update_link_subscription(self) -> None:
        desired = self._current_linked_entity_id()
        if desired == self._linked_entity_id:
            return
        if self._linked_unsub is not None:
            self._linked_unsub()
            self._linked_unsub = None
        self._linked_entity_id = desired
        if desired and self.hass is not None:
            # Register a thread-safe callback on the event loop, not in an executor
            self._linked_unsub = async_track_state_change_event(
                self.hass, [desired], self._handle_linked_state_change
            )

    @callback
    def _handle_linked_state_change(self, _event: object) -> None:
        """Handle state changes from the linked source entity on the event loop."""
        self.async_write_ha_state()

    @callback
    def _handle_output_poll_update(self) -> None:
        """Handle updates from the rolling poll (volume/mute/source changes)."""
        self._update_link_subscription()
        self.async_write_ha_state()

    def _linked_attr(self, key: str) -> Any | None:
        if not self._linked_entity_id or self.hass is None:
            return None
        st = self._state_getter(self.hass, self._linked_entity_id)
        if not st:
            return None
        return st.attributes.get(key)

    def _available_linked_integration_players(self) -> list[str]:
        """Return speaker media_players from the linked input's integration."""
        if not self._linked_entity_id or self.hass is None:
            return []

        linked_state = self.hass.states.get(self._linked_entity_id)
        if not linked_state:
            return []

        candidates = [self._linked_entity_id]
        linked_members = linked_state.attributes.get("group_members")
        if isinstance(linked_members, list):
            candidates.extend(linked_members)

        available: list[str] = []
        for entity_id in candidates:
            state = self.hass.states.get(entity_id)
            if not state:
                continue
            device_class = state.attributes.get("device_class")
            if device_class not in (
                MediaPlayerDeviceClass.SPEAKER,
                MediaPlayerDeviceClass.SPEAKER.value,
            ):
                continue
            available.append(entity_id)

        return available

    def _available_output_players(self) -> list[str]:
        """Return available Triad output players that are not routed."""
        if self.hass is None:
            return []

        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._entry.entry_id)

        available: list[str] = []
        for entry in entries:
            if entry.platform != DOMAIN:
                continue
            if entry.entity_id == self.entity_id:
                continue
            if not entry.unique_id or not re.search(r"_output_\d+$", entry.unique_id):
                continue

            state = self.hass.states.get(entry.entity_id)
            if not state:
                continue
            device_class = state.attributes.get("device_class")
            if device_class not in (
                MediaPlayerDeviceClass.SPEAKER,
                MediaPlayerDeviceClass.SPEAKER.value,
            ):
                continue
            # Consider available only when not connected to any input/source
            if state.attributes.get("source"):
                continue

            available.append(entry.entity_id)

        return available

    def _available_join_players(self) -> list[str]:
        """Return entities available for joining this player."""
        candidates = []
        candidates.extend(self._available_output_players())
        candidates.extend(self._available_linked_integration_players())

        seen: set[str] = set()
        unique: list[str] = []
        for entity_id in candidates:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            unique.append(entity_id)
        return unique

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose additional attributes including join candidates."""
        available = self._available_join_players()
        if not available:
            return {}
        return {"available_join_players": available}

    # ---- Media info ----
    @property
    def media_title(self) -> str | None:
        """Return the current media title from the linked source, if any."""
        return self._linked_attr("media_title")

    @property
    def media_artist(self) -> str | None:
        """Return the media artist from the linked source, if any."""
        return self._linked_attr("media_artist")

    @property
    def media_album_name(self) -> str | None:
        """Return the media album from the linked source, if any."""
        return self._linked_attr("media_album_name")

    @property
    def media_duration(self) -> int | None:
        """Return the media duration (seconds) from the linked source, if any."""
        return self._linked_attr("media_duration")

    @property
    def media_content_id(self) -> str | None:
        """Return the media content id from the linked source, if any."""
        return self._linked_attr("media_content_id")

    @property
    def media_content_type(self) -> str | None:
        """Return the media content type from the linked source, if any."""
        return self._linked_attr("media_content_type")

    @property
    def entity_picture(self) -> str | None:
        """Return the artwork URL from the linked source, if any."""
        return self._linked_attr("entity_picture")

    # ---- Core media player properties and commands ----
    @property
    def state(self) -> str:
        """Return the state of the entity (STATE_ON or STATE_OFF)."""
        return MediaPlayerState.ON if self.is_on else MediaPlayerState.OFF

    @property
    def source(self) -> str | None:
        """Return the current source name."""
        return self.output.source_name

    @property
    def source_list(self) -> list[str]:
        """Return the list of available source names."""
        return self.output.source_list

    @property
    def is_on(self) -> bool:
        """Return True if the output is on, False if off."""
        return self.output.is_on

    @property
    def volume_level(self) -> float | None:
        """Return the volume level of the output (0..1), or None if unknown."""
        return self.output.volume if self.output.volume is not None else None

    @property
    def is_volume_muted(self) -> bool | None:
        """Return True if the output is muted."""
        return self.output.muted

    async def async_select_source(self, source: str) -> None:
        """Select a source by friendly name."""
        input_id = self.output.source_id_for_name(source)
        if input_id is not None:
            _LOGGER.info(
                "Selecting source '%s' for output %d",
                source,
                self.output.number,
            )
            await self.output.set_source(input_id)
            # Update link subscription first so derived attributes reflect
            # the new linked source on this state write.
            self._update_link_subscription()
            self.async_write_ha_state()
        else:
            _LOGGER.error("Unknown source name: %s", source)

    async def async_added_to_hass(self) -> None:
        """Entity added to Home Assistant: seed and write initial state."""
        self._update_link_subscription()
        # Subscribe first so any async refresh updates the state when done
        self._output_unsub = self.output.add_listener(self._handle_output_poll_update)
        # Queue an initial refresh without blocking entity setup
        if self.hass is not None:
            self.hass.async_create_task(self.output.refresh_and_notify())
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Entity will be removed from Home Assistant: clean up."""
        if self._output_unsub is not None:
            self._output_unsub()
            self._output_unsub = None

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level of the output (0..1)."""
        _LOGGER.info("Setting volume for output %d to %.2f", self.output.number, volume)
        await self.output.set_volume(volume)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:  # noqa: FBT001
        """Mute or unmute the output."""
        _LOGGER.info("Setting mute for output %d to %s", self.output.number, mute)
        await self.output.set_muted(muted=mute)
        self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        """Step the volume up one unit."""
        _LOGGER.info("Volume UP (step) on output %d", self.output.number)
        await self.output.volume_up_step(large=False)
        await self.output.refresh()
        self.async_write_ha_state()

    async def async_volume_down(self) -> None:
        """Step the volume down one unit."""
        _LOGGER.info("Volume DOWN (step) on output %d", self.output.number)
        await self.output.volume_down_step(large=False)
        await self.output.refresh()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off the output (disconnect from any input)."""
        _LOGGER.info("Turning OFF output %d", self.output.number)
        await self.output.turn_off()
        # Unsubscribe before writing state so we don't expose linked metadata
        # while the output is off.
        self._update_link_subscription()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn on the player in UI without routing a source."""
        _LOGGER.info("Turning ON output %d", self.output.number)
        await self.output.turn_on()
        self._update_link_subscription()
        self.async_write_ha_state()

    async def async_turn_on_with_source(self, input_entity_id: str) -> None:
        """Turn on this output and route the given source."""
        # Map input entity ID to input number
        # First check if input_entity_id is one of our input entities
        if self.hass is not None:
            registry = er.async_get(self.hass)
            input_entry = registry.async_get(input_entity_id)

            if (
                input_entry
                and input_entry.platform == DOMAIN
                and input_entry.config_entry_id == self._entry.entry_id
            ):
                # This is one of our input entities - extract input number from unique_id  # noqa: E501
                # Format: {entry_id}_input_{number}
                match = re.search(r"_input_(\d+)$", input_entry.unique_id)
                if match:
                    try:
                        source = int(match.group(1))
                        if source in self._options.get("active_inputs", []):
                            await self.output.set_source(source)
                            self._update_link_subscription()
                            self.async_write_ha_state()
                            return
                    except ValueError:
                        pass

        # Fall back to checking input_links for linked entities
        input_links = self._input_links
        source = None
        for input_str, linked_entity_id in input_links.items():
            if linked_entity_id == input_entity_id:
                try:
                    source = int(input_str)
                    break
                except ValueError:
                    pass

        if source is None:
            msg = f"Input entity {input_entity_id} is not linked in integration options"
            raise ValueError(msg)

        if source not in self._options.get("active_inputs", []):
            msg = f"Input {source} is not active"
            raise ServiceValidationError(msg)

        await self.output.set_source(source)
        await self.output.turn_on()
        self._update_link_subscription()
        self.async_write_ha_state()

    async def async_unjoin_player(self) -> None:
        """Leave any current group by disconnecting from the source."""
        _LOGGER.info("Unjoining output %d from group", self.output.number)
        await self.output.turn_off()
        self._update_link_subscription()
        self.async_write_ha_state()


class TriadAmsSourceMediaPlayer(MediaPlayerEntity):
    """Media player entity that proxies a linked input media_player."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        input_model: TriadAmsInput,
        entry: ConfigEntry,
        group_members: list[str] | None = None,
    ) -> None:
        """Initialize a Triad AMS input proxy media player entity."""
        self.input = input_model
        self._entry = entry
        self._group_members = group_members or []
        self._attr_unique_id = f"{entry.entry_id}_input_{input_model.number}"
        self._attr_name = input_model.name
        self._attr_has_entity_name = True
        self._attr_device_class = MediaPlayerDeviceClass.RECEIVER
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Triad",
            "model": "Audio Matrix",
        }
        self._linked_unsub: callable | None = None

    @callback
    def _handle_linked_state_change(self, _event: object) -> None:
        """Handle state changes from the linked source entity on the event loop."""
        self.async_write_ha_state()

    @property
    def state(self) -> str:
        """Proxy linked entity state (ON/OFF)."""
        if not self.input.linked_entity_id or self.hass is None:
            return MediaPlayerState.OFF
        st = self.hass.states.get(self.input.linked_entity_id)
        if not st or st.state == "unknown":
            return MediaPlayerState.OFF
        return st.state

    @property
    def supported_features(self) -> int:
        """Proxy supported features from the linked entity and add grouping support."""
        linked_features = 0
        if self.input.linked_entity_id and self.hass is not None:
            st = self.hass.states.get(self.input.linked_entity_id)
            if st:
                linked_features = int(st.attributes.get("supported_features", 0))
        return linked_features | MediaPlayerEntityFeature.GROUPING

    @property
    def group_members(self) -> list[str] | None:
        """Return combined group members from linked entity and output entities."""
        members = list(self._group_members) if self._group_members else []
        _LOGGER.debug("%s - Current group members: %s", self._attr_name, members)

        # Add linked entity's group members
        if self.input.linked_entity_id and self.hass is not None:
            st = self.hass.states.get(self.input.linked_entity_id)
            if st:
                linked_members = st.attributes.get("group_members")
                if linked_members:
                    _LOGGER.debug(
                        "%s - Adding linked group members: %s",
                        self._attr_name,
                        linked_members,
                    )
                    members.extend(linked_members)

        return members if members else None

    @group_members.setter
    def group_members(self, members: list[str]) -> None:
        """Set the list of group members."""
        self._group_members = members

    @property
    def group_leader(self) -> bool:
        """Return whether input is a group leader."""
        return len(self._group_members) > 0

    def _linked_attr(self, key: str) -> Any | None:
        if not self.input.linked_entity_id or self.hass is None:
            return None
        st = self.hass.states.get(self.input.linked_entity_id)
        if not st:
            return None
        return st.attributes.get(key)

    def _available_routable_outputs(self) -> list[str]:
        """Return Triad output speakers available for routing to this input."""
        if self.hass is None:
            return []

        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._entry.entry_id)

        available: list[str] = []
        for entry in entries:
            if entry.platform != DOMAIN:
                continue
            if not entry.unique_id or not re.search(r"_output_\d+$", entry.unique_id):
                continue

            state = self.hass.states.get(entry.entity_id)
            if not state:
                continue
            device_class = state.attributes.get("device_class")
            if device_class not in (
                MediaPlayerDeviceClass.SPEAKER,
                MediaPlayerDeviceClass.SPEAKER.value,
            ):
                continue

            available.append(entry.entity_id)

        return available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose additional attributes including routable outputs."""
        available = self._available_routable_outputs()
        if not available:
            return {}
        return {"available_routable_players": available}

    # ---- Media info ----
    @property
    def media_title(self) -> str | None:
        """Return the current media title from the linked source, if any."""
        return self._linked_attr("media_title")

    @property
    def media_artist(self) -> str | None:
        """Return the media artist from the linked source, if any."""
        return self._linked_attr("media_artist")

    @property
    def media_album_name(self) -> str | None:
        """Return the media album from the linked source, if any."""
        return self._linked_attr("media_album_name")

    @property
    def media_duration(self) -> int | None:
        """Return the media duration (seconds) from the linked source, if any."""
        return self._linked_attr("media_duration")

    @property
    def media_content_id(self) -> str | None:
        """Return the media content id from the linked source, if any."""
        return self._linked_attr("media_content_id")

    @property
    def media_content_type(self) -> str | None:
        """Return the media content type from the linked source, if any."""
        return self._linked_attr("media_content_type")

    @property
    def entity_picture(self) -> str | None:
        """Return the artwork URL from the linked source, if any."""
        return self._linked_attr("entity_picture")

    async def async_added_to_hass(self) -> None:
        """Subscribe to linked entity state changes to update our state."""
        if self.input.linked_entity_id and self.hass is not None:
            self._linked_unsub = async_track_state_change_event(
                self.hass,
                [self.input.linked_entity_id],
                self._handle_linked_state_change,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from linked entity state changes."""
        if self._linked_unsub is not None:
            self._linked_unsub()
            self._linked_unsub = None

    async def async_join_players(self, group_members: list[str]) -> None:
        """Route provided output players to this input."""
        if self.hass is None:
            return
        if not group_members:
            # Empty list means unjoin all members
            self._group_members = []
            self.async_write_ha_state()
            return

        registry = er.async_get(self.hass)
        valid_members: list[str] = []

        for member in group_members:
            entry = registry.async_get(member)
            if entry is None or entry.platform != DOMAIN:
                msg = f"Entity {member} is not a Triad AMS output"
                raise ServiceValidationError(msg)
            valid_members.append(member)

        tasks = [
            self.hass.services.async_call(
                DOMAIN,
                "turn_on_with_source",
                {
                    "entity_id": member,
                    "input_entity_id": self.entity_id,
                },
                blocking=True,
            )
            for member in valid_members
        ]

        if tasks:
            await asyncio.gather(*tasks)
        new_members = list(self._group_members)
        for member in valid_members:
            if member not in new_members:
                new_members.append(member)

        self._group_members = new_members
        # Refresh state to update extra_state_attributes with new routable players list
        self.async_write_ha_state()

    # The unjoin operation lives on member entities (TriadAmsMediaPlayer).
