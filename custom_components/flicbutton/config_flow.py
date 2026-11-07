"""Config flow for the Flic button (flicd) integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CLICK_TYPES,
    CONF_AUTO_PAIR,
    CONF_IGNORED_CLICK_TYPES,
    CONF_QUEUE_TIMEOUT,
    CONNECT_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_QUEUE_TIMEOUT,
    DOMAIN,
)
from .flicd.client import FlicdClient, FlicdConnectionError, ServerInfo
from .flicd.packets import ScanWizardResult
from .hub import FlicHub, HubOptions, PairingInProgress

_PAIR_REASONS: dict[ScanWizardResult, str] = {
    ScanWizardResult.WizardSuccess: "pair_success",
    ScanWizardResult.WizardFailedTimeout: "pair_timeout",
    ScanWizardResult.WizardButtonIsPrivate: "pair_private",
    ScanWizardResult.WizardBluetoothUnavailable: "pair_bluetooth_unavailable",
    ScanWizardResult.WizardInternetBackendError: "pair_backend_error",
    ScanWizardResult.WizardInvalidData: "pair_invalid_data",
    ScanWizardResult.WizardButtonBelongsToOtherPartner: "pair_other_partner",
    ScanWizardResult.WizardButtonAlreadyConnectedToOtherDevice: (
        "pair_connected_elsewhere"
    ),
    ScanWizardResult.WizardCancelledByUser: "pair_cancelled",
}

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FlicOptionsFlow:
        """Return the options flow."""
        return FlicOptionsFlow()

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
            except Exception:
                _LOGGER.exception(
                    "Unexpected error talking to flicd at %s:%s", host, port
                )
                errors["base"] = "unknown"
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


class FlicOptionsFlow(OptionsFlowWithReload):
    """Settings and on-demand pairing."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._reason = "not_connected"
        self._placeholders: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "pair"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_QUEUE_TIMEOUT: int(user_input[CONF_QUEUE_TIMEOUT]),
                    CONF_IGNORED_CLICK_TYPES: list(
                        user_input[CONF_IGNORED_CLICK_TYPES]
                    ),
                    CONF_AUTO_PAIR: bool(user_input[CONF_AUTO_PAIR]),
                }
            )
        current = HubOptions.from_entry_options(self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_QUEUE_TIMEOUT, default=DEFAULT_QUEUE_TIMEOUT
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=60,
                        step=1,
                        unit_of_measurement=UnitOfTime.SECONDS,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_IGNORED_CLICK_TYPES, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=CLICK_TYPES,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key="click_types",
                    )
                ),
                vol.Required(CONF_AUTO_PAIR, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_QUEUE_TIMEOUT: current.queue_timeout,
                    CONF_IGNORED_CLICK_TYPES: current.ignored_click_types,
                    CONF_AUTO_PAIR: current.auto_pair,
                },
            ),
        )

    async def _async_pair(self, hub: FlicHub) -> None:
        """Run the wizard and store the outcome as an abort reason."""
        try:
            result = await hub.async_pair()
        except PairingInProgress:
            self._reason = "pair_in_progress"
        except FlicdConnectionError:
            self._reason = "not_connected"
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # The hub was stopped (entry unloaded) under us.
            self._reason = "not_connected"
        else:
            self._reason = _PAIR_REASONS.get(result.result, "pair_invalid_data")
            if result.bd_addr is not None:
                self._placeholders = {"bd_addr": result.bd_addr}

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._task is None:
            hub: FlicHub = self.config_entry.runtime_data
            if not hub.connected:
                return self.async_abort(reason="not_connected")
            if HubOptions.from_entry_options(self.config_entry.options).auto_pair:
                return self.async_abort(reason="auto_pair_enabled")
            self._task = self.hass.async_create_task(
                self._async_pair(hub), "flicbutton options pairing"
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="pair", progress_action="pairing", progress_task=self._task
            )
        return self.async_show_progress_done(next_step_id="pair_result")

    async def async_step_pair_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason=self._reason, description_placeholders=self._placeholders
        )

    @callback
    def async_remove(self) -> None:
        """Cancel a running wizard when the dialog is closed early."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
