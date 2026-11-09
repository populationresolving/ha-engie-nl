"""Diagnostics with account details redacted."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import EngieConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "cookies", "ean", "id", "reference", "attachment", "description"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EngieConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "flags": data.flags if data else None,
            "metering_points": [json.loads(json.dumps(asdict(p), default=str)) for p in data.metering_points] if data else None,
            "registers": {k: len(v) for k, v in coordinator.history.history.items()},
            "consumption": data.consumption if data else None,
            "estimations": data.estimations if data else None,
            "transactions": data.transactions[:3] if data else None,
        },
        TO_REDACT,
    )
