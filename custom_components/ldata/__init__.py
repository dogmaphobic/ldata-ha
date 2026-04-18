"""The LDATA integration."""
from __future__ import annotations
import logging

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, LOGGER_NAME
from .coordinator import LDATAUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]
_LOGGER = logging.getLogger(LOGGER_NAME)

SERVICE_RESET_PANEL = "reset_panel_energy"
ATTR_DEVICE_ID = "device_id"

SERVICE_RESET_PANEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
    }
)


def _reset_energy_entity(entity, entity_id: str) -> str:
    """Reset one live energy entity and return a summary string.

    Daily sensors already expose async_reset_baseline(); use that native path.
    Lifetime sensors in this branch publish a monotonic cached value, so clear
    their in-memory state to let the next coordinator update accept the new
    hardware value after a panel reset.
    """
    current = getattr(entity, "_state", None)

    if hasattr(entity, "async_reset_baseline"):
        return f"{entity_id}: daily baseline cleared (was {current if current is not None else '?'} kWh)"

    if hasattr(entity, "_state"):
        entity._state = None
        entity.async_write_ha_state()
        return f"{entity_id}: lifetime cache cleared (was {current if current is not None else '?'} kWh)"

    return f"{entity_id}: skipped (entity does not expose resettable energy state)"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LDATA from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    
    # Handle backward compatibility for username/email field
    username = entry.data.get("email", entry.data.get(CONF_USERNAME))

    coordinator = LDATAUpdateCoordinator(
        hass,
        username,
        entry.data[CONF_PASSWORD],
        entry,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up a listener for options updates
    entry.add_update_listener(options_update_listener)

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_PANEL):
        async def handle_reset_panel_energy(call: ServiceCall) -> None:
            """Reset all live energy sensors associated with one panel."""
            target_device_id = call.data[ATTR_DEVICE_ID]
            dev_reg = dr.async_get(hass)
            ent_reg = er.async_get(hass)
            comp = hass.data.get("entity_components", {}).get("sensor")

            device = dev_reg.async_get(target_device_id)
            if not device:
                _LOGGER.error("reset_panel_energy: device %s not found", target_device_id)
                return

            panel_id: str | None = None
            for identifier in device.identifiers:
                if len(identifier) >= 2 and identifier[0] == DOMAIN:
                    if len(identifier) == 3:
                        panel_id = identifier[1]
                        break

                    candidate = identifier[1]
                    for coordinator in hass.data.get(DOMAIN, {}).values():
                        if not isinstance(coordinator, LDATAUpdateCoordinator) or not coordinator.data:
                            continue

                        for panel in coordinator.data.get("panels", []):
                            if panel.get("serialNumber") == candidate or panel.get("id") == candidate:
                                panel_id = panel.get("id") or panel.get("serialNumber")
                                break
                        if panel_id:
                            break

                        for breaker_data in coordinator.data.get("breakers", {}).values():
                            if breaker_data.get("serialNumber") == candidate:
                                panel_id = breaker_data.get("panel_id")
                                break
                        if panel_id:
                            break

            if not panel_id:
                _LOGGER.error(
                    "reset_panel_energy: could not resolve panel for device '%s' (%s)",
                    device.name,
                    target_device_id,
                )
                return

            panel_device_identifiers: set[tuple] = {(DOMAIN, panel_id)}
            for coordinator in hass.data.get(DOMAIN, {}).values():
                if not isinstance(coordinator, LDATAUpdateCoordinator) or not coordinator.data:
                    continue

                for panel in coordinator.data.get("panels", []):
                    if panel.get("id") == panel_id or panel.get("serialNumber") == panel_id:
                        if panel.get("serialNumber"):
                            panel_device_identifiers.add((DOMAIN, panel["serialNumber"]))
                        if panel.get("id"):
                            panel_device_identifiers.add((DOMAIN, panel["id"]))

                for breaker_data in coordinator.data.get("breakers", {}).values():
                    if breaker_data.get("panel_id") == panel_id and breaker_data.get("serialNumber"):
                        panel_device_identifiers.add((DOMAIN, breaker_data["serialNumber"]))

                for ct_id, ct_data in coordinator.data.get("cts", {}).items():
                    if ct_data.get("panel_id") == panel_id:
                        panel_device_identifiers.add((DOMAIN, panel_id, ct_id))

            reset_lines: list[str] = []
            for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
                if not isinstance(coordinator, LDATAUpdateCoordinator):
                    continue
                for ent_entry in er.async_entries_for_config_entry(ent_reg, entry_id):
                    if ent_entry.domain != "sensor" or not ent_entry.device_id:
                        continue
                    ent_device = dev_reg.async_get(ent_entry.device_id)
                    if not ent_device:
                        continue
                    if not ent_device.identifiers.intersection(panel_device_identifiers):
                        continue
                    if comp is None:
                        continue

                    entity = comp.get_entity(ent_entry.entity_id)
                    if entity is None:
                        continue

                    if hasattr(entity, "async_reset_baseline"):
                        await entity.async_reset_baseline()
                        reset_lines.append(
                            f"{ent_entry.entity_id}: daily baseline cleared"
                        )
                    elif getattr(entity, "_attr_device_class", None) in (SensorDeviceClass.ENERGY, "energy") or getattr(entity, "device_class", None) in (SensorDeviceClass.ENERGY, "energy"):
                        reset_lines.append(_reset_energy_entity(entity, ent_entry.entity_id))

            if reset_lines:
                _LOGGER.warning(
                    "reset_panel_energy: reset %d energy sensors for panel %s (%s):\n  %s",
                    len(reset_lines),
                    device.name,
                    panel_id,
                    "\n  ".join(reset_lines),
                )
            else:
                _LOGGER.warning(
                    "reset_panel_energy: no live energy sensors found for panel %s (%s)",
                    device.name,
                    panel_id,
                )

        hass.services.async_register(
            DOMAIN,
            SERVICE_RESET_PANEL,
            handle_reset_panel_energy,
            schema=SERVICE_RESET_PANEL_SCHEMA,
        )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Gracefully shutdown WebSocket before unloading
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()
    
    # Use the built-in unload method
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Also pop the coordinator from hass.data
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        
    return unload_ok

async def options_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
