"""Config flow for the Flic button (flicd) integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import CONNECT_TIMEOUT, DEFAULT_PORT, DOMAIN
from .flicd.client import FlicdClient, FlicdConnectionError, ServerInfo

_LOGGER = logging.getLogger(__name__)


async def _async_get_server_info(host: str, port: int) -> ServerInfo:
    """Connect to flicd and return its server info."""
    client = FlicdClient(host, port)
    await client.connect(CONNECT_TIMEOUT)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            return await client.get_info()
    finally:
        await client.close()


class FlicButtonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a flicd server."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            try:
                info = await _async_get_server_info(host, port)
            except (FlicdConnectionError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info.bd_addr.lower())
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port}
                )
                return self.async_create_entry(
                    title=f"flicd {host}",
                    data={CONF_HOST: host, CONF_PORT: port},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    }
                ),
                user_input,
            ),
            errors=errors,
        )
