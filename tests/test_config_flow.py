"""Tests for the flicbutton config flow."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flicbutton.const import DOMAIN
from custom_components.flicbutton.flicd.packets import (
    BluetoothControllerState,
    ScanWizardResult,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _start(hass: HomeAssistant, host: str, port: int):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: host, CONF_PORT: port}
    )


async def _unload_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_flicd) -> None:
    result = await _start(hass, "127.0.0.1", fake_flicd.port)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "flicd 127.0.0.1"
    assert result["data"] == {"host": "127.0.0.1", "port": fake_flicd.port}
    assert result["options"] == {}
    entry = result["result"]
    assert entry.unique_id == fake_flicd.bd_addr.lower()
    await hass.async_block_till_done()
    await _unload_all(hass)


async def test_user_flow_cannot_connect(hass: HomeAssistant, fake_flicd) -> None:
    # Reserve a port that nothing listens on.
    dead = fake_flicd.port
    await fake_flicd.stop()
    result = await _start(hass, "127.0.0.1", dead)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    await fake_flicd.start()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_flicd.port}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await _unload_all(hass)


async def test_user_flow_already_configured_updates_host(
    hass: HomeAssistant, fake_flicd
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr.lower(),
        data={CONF_HOST: "192.0.2.1", CONF_PORT: 5551},
    )
    entry.add_to_hass(hass)

    result = await _start(hass, "127.0.0.1", fake_flicd.port)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {CONF_HOST: "127.0.0.1", CONF_PORT: fake_flicd.port}


# -- options flow -------------------------------------------------------------

C = "80:e4:da:71:2b:99"


def _wizard(result: ScanWizardResult, *extra) -> list:
    return [*extra, ("EvtScanWizardCompleted", {"result": result})]


@pytest.fixture
async def loaded(hass: HomeAssistant, fake_flicd, request):
    options = getattr(request, "param", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr.lower(),
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_flicd.port},
        options=options,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def _pair(hass: HomeAssistant, entry) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "pair"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pair"}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        assert result["progress_action"] == "pairing"
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    return result


async def test_options_settings_saved(hass: HomeAssistant, loaded) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"queue_timeout": 10, "ignored_click_types": ["hold"], "auto_pair": True},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options == {
        "queue_timeout": 10,
        "ignored_click_types": ["hold"],
        "auto_pair": True,
    }
    reload.assert_called_once_with(loaded.entry_id)
    assert loaded.state is ConfigEntryState.LOADED

    # The saved values are the suggested values of the next dialog.
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    suggested = {
        str(k): k.description["suggested_value"] for k in result["data_schema"].schema
    }
    assert suggested == {
        "queue_timeout": 10,
        "ignored_click_types": ["hold"],
        "auto_pair": True,
    }
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_pair_success(hass: HomeAssistant, loaded, fake_flicd) -> None:
    fake_flicd.scan_wizard_script = _wizard(
        ScanWizardResult.WizardSuccess,
        ("EvtScanWizardFoundPublicButton", {"bd_addr": C, "name": "F0"}),
        ("EvtNewVerifiedButton", {"bd_addr": C}),
    )
    result = await _pair(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pair_success"
    assert result["description_placeholders"] == {"bd_addr": C}
    await hass.async_block_till_done()
    assert (
        dr.async_get(hass).async_get_device_by_identifier((DOMAIN, C), loaded.entry_id)
        is not None
    )


@pytest.mark.parametrize(
    ("wizard_result", "reason"),
    [
        (ScanWizardResult.WizardFailedTimeout, "pair_timeout"),
        (ScanWizardResult.WizardButtonIsPrivate, "pair_private"),
        (ScanWizardResult.WizardBluetoothUnavailable, "pair_bluetooth_unavailable"),
        (ScanWizardResult.WizardInternetBackendError, "pair_backend_error"),
        (ScanWizardResult.WizardInvalidData, "pair_invalid_data"),
        (ScanWizardResult.WizardButtonBelongsToOtherPartner, "pair_other_partner"),
        (
            ScanWizardResult.WizardButtonAlreadyConnectedToOtherDevice,
            "pair_connected_elsewhere",
        ),
        (ScanWizardResult.WizardCancelledByUser, "pair_cancelled"),
    ],
)
async def test_pair_results(
    hass: HomeAssistant, loaded, fake_flicd, wizard_result, reason
) -> None:
    fake_flicd.scan_wizard_script = _wizard(wizard_result)
    result = await _pair(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_pair_timeout(hass: HomeAssistant, loaded, fake_flicd) -> None:
    fake_flicd.scan_wizard_script = _wizard(ScanWizardResult.WizardFailedTimeout)
    assert (await _pair(hass, loaded))["reason"] == "pair_timeout"


async def test_pair_private(hass: HomeAssistant, loaded, fake_flicd) -> None:
    fake_flicd.scan_wizard_script = _wizard(ScanWizardResult.WizardButtonIsPrivate)
    assert (await _pair(hass, loaded))["reason"] == "pair_private"


async def test_pair_bluetooth_unavailable(
    hass: HomeAssistant, loaded, fake_flicd
) -> None:
    fake_flicd.controller_state = BluetoothControllerState.Detached
    fake_flicd.scan_wizard_script = _wizard(ScanWizardResult.WizardBluetoothUnavailable)
    assert (await _pair(hass, loaded))["reason"] == "pair_bluetooth_unavailable"


async def test_pair_not_connected(hass: HomeAssistant, loaded, fake_flicd) -> None:
    await fake_flicd.drop_clients()
    await loaded.runtime_data.async_stop()
    result = await _pair(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_connected"


@pytest.mark.parametrize("loaded", [{"auto_pair": True}], indirect=True)
async def test_pair_refused_with_auto_pair(hass: HomeAssistant, loaded) -> None:
    result = await _pair(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "auto_pair_enabled"


async def test_pair_in_progress(hass: HomeAssistant, loaded, fake_flicd) -> None:
    # The wizard never completes, so it stays pending.
    hub = loaded.runtime_data
    manual = asyncio.create_task(hub.async_pair())
    await fake_flicd.wait_for_command("CmdCreateScanWizard")
    result = await _pair(hass, loaded)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pair_in_progress"
    manual.cancel()
    await asyncio.gather(manual, return_exceptions=True)


async def test_pair_connection_lost(hass: HomeAssistant, loaded, fake_flicd) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pair"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake_flicd.wait_for_command("CmdCreateScanWizard")
    await fake_flicd.drop_clients()
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_connected"


async def test_pair_hub_stopped(hass: HomeAssistant, loaded, fake_flicd) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pair"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake_flicd.wait_for_command("CmdCreateScanWizard")
    await loaded.runtime_data.async_stop()
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_connected"


async def test_pair_closed_early_cancels_wizard(
    hass: HomeAssistant, loaded, fake_flicd
) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pair"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    create = await fake_flicd.wait_for_command("CmdCreateScanWizard")
    hass.config_entries.options.async_abort(result["flow_id"])
    cancel = await fake_flicd.wait_for_command("CmdCancelScanWizard")
    assert cancel["scan_wizard_id"] == create["scan_wizard_id"]
    await hass.async_block_till_done()
