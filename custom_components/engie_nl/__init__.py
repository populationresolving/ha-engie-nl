"""ENGIE Nederland (unofficial): Mijn ENGIE rates, contract, usage and bills."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import EngieConfigEntry, EngieCoordinator
from .crypto import EngieCipher
from .statistics import ReadingHistory

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EngieConfigEntry) -> bool:
    cipher = await EngieCipher.async_load(hass)
    coordinator = EngieCoordinator(hass, entry, cipher)
    await coordinator.history.async_load()
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_close()
        raise
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EngieConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EngieConfigEntry) -> None:
    """Forget the stored reading history (statistics stay in the recorder)."""
    await ReadingHistory(hass, entry.entry_id).async_remove()
