# flicbutton — Home Assistant custom integration for flicd

Date: 2026-11-07
Owner: frawau (GitHub)
Status: ready for owner approval

## 1. Intent and why

Home Assistant core ships `flic` ("Flic (legacy)", `quality_scale: legacy`,
no codeowner). It is a single YAML `binary_sensor` platform built on the
threaded `pyflic==2.0.4` client. It talks to a `flicd` daemon
(50ButtonsEach/fliclib-linux-hci) over TCP.

The owner runs flicd on a separate Raspberry Pi Zero W. The concrete pain:
**when the Pi reboots, the buttons stay dead until Home Assistant is
restarted.** The legacy code connects once at startup, has no reconnect, and
its connection-status handler is never wired up.

`flicbutton` is a modern replacement, configured through the UI, that heals
itself. It will be published in the **Home Assistant Marketplace main
catalog** (the built-in successor of HACS since HA 2026.11) so other flicd
users can install it.

Success means:
- Rebooting the flicd host requires no action in HA. Buttons go
  `unavailable` while it is down and work again once it is back.
- The owner's existing automations, which trigger on `flic_click` events,
  keep working unchanged.
- Setup, pairing and options are all done in the UI. No YAML.
- The repo passes the Marketplace and hassfest validation and is accepted
  into the default catalog.

## 2. Goals

1. Config flow: host + port (default 5551), with a connection test. One
   config entry per flicd server; several servers are supported.
2. Every button verified by flicd becomes a HA device, with:
   - an `event` entity (event types `single`, `double`, `hold`)
   - a `binary_sensor` reflecting pressed/released (kept from legacy; some
     users rely on it for "while held" logic)
   - a battery `sensor` (%, device_class battery) via flicd's battery status
     listener; `-1` from flicd maps to unknown
   - device info from `CmdGetButtonInfo`: serial number, color, Flic version
     (1 or 2), firmware version. The model name is "Flic 1" or "Flic 2".
3. Device triggers ("<button> single/double/hold pressed") in the automation
   editor.
4. Backward compatible bus event `flic_click`, same name and payload keys as
   legacy: `button_name`, `button_address`, `click_type`
   (`single`/`double`/`hold`), `queued_time` (seconds, int).
   `button_name` stays fixed at the legacy-style value `flic_<mac without
   colons, lowercase>` and does **not** follow UI renames, so existing
   automations never break.
5. On-demand pairing: a "Pair a new button" action that runs flicd's scan
   wizard once, shows progress and reports the result ("Added Flic
   XX:XX:…", "Button is private: hold it 7 s and retry", "Nothing found",
   "flicd has no Bluetooth controller", "Flic backend unreachable", …).
6. Options flow:
   - queued-event timeout in seconds (default 3, as in legacy). Click events
     that flicd queued for longer are dropped with a warning.
   - ignored click types (multi-select of single/double/hold)
   - continuous auto-pairing (a scan wizard restarted in a loop, like legacy
     `discovery: true`), **off by default**.
7. Self-healing connection:
   - automatic reconnect with exponential backoff, 1 s growing to a 60 s cap
   - application-level keepalive using `CmdPing` (every 30 s, with a
     10 s answer timeout), so half-open TCP connections are detected
     (a Pi losing power does not send a FIN)
   - while disconnected, all entities of that entry are `unavailable`
   - after reconnect: `CmdGetInfo` → re-create connection channels and
     battery listeners for all verified buttons, and add any new buttons
   - first setup failing to connect raises `ConfigEntryNotReady`, so HA
     retries on its own
8. A hub device per config entry, representing the flicd server, with a
   connectivity `binary_sensor` and a diagnostic `sensor` for the Bluetooth
   controller state (`detached`/`resetting`/`attached`). Button devices link
   to it via `via_device`.
9. Deleting a button device in HA (`async_remove_config_entry_device`)
   sends `CmdDeleteButton` to flicd, so the button is unpaired and does not
   come back. If flicd deletes a button (`EvtButtonDeleted`, e.g. factory
   reset), its HA device is removed.
10. Diagnostics download (config entry diagnostics: server info, button
    list, connection state; redact nothing sensitive beyond host).
11. Translations: English (`translations/en.json`; custom integrations do not ship `strings.json`).

## 3. Non-goals (this phase)

- Running flicd inside HA. That is **phase 2**, see §8.
- Importing legacy YAML `flic:` config. It's a separate domain, so users add
  it once through the UI.
- Flic Hub (the commercial hub, a different protocol) and direct Bluetooth
  without flicd.
- Roaming buttons, i.e. one button shared by several flicd servers through a
  shared `--my-bdaddr` and a copied database. Rejected by the owner as too
  complicated for most users. **A button is paired to exactly one flicd
  instance.** You get more range by adding more servers (one config entry
  each), and each server has its own buttons.
- Upstreaming to HA core.
- Latency mode tuning, raw `ButtonScanner` advertisement exposure,
  per-button click-type ignores.

## 4. Anti-goals

- Anything that makes the owner restart HA after a flicd outage.
- Changing the `flic_click` payload or names. Existing automations must
  keep working.
- Pairing buttons nobody asked for. Continuous scanning stays opt-in.
- Blocking I/O or threads in the event loop (no `pyflic`, no
  `threading.Thread`).
- Overriding the core `flic` domain.

## 5. Constraints

- **Domain:** `flicbutton`. Display name: "Flic button (flicd)". It sits
  alongside core `flic` without overriding it.
- **Repo:** `github.com/frawau/flicbutton` (public). License MIT.
- **Minimum HA version:** 2026.11.0 (declared in `hacs.json`
  `homeassistant`).
- **Marketplace / HACS compliance.** Community sources only; re-check
  against official 2026.11 docs during implementation:
  - layout: `custom_components/flicbutton/…` and nothing else under
    `custom_components/`
  - `hacs.json` with `name`, `homeassistant`; no undocumented keys
  - `manifest.json`: `domain`, `name`, `version` (SemVer, matching the
    release tag), `documentation`, `issue_tracker`, `codeowners:
    ["@frawau"]`, `config_flow: true`, `iot_class: local_push`,
    `integration_type: hub`, `requirements: []`, `dependencies: []`
    (plus whatever hassfest demands)
  - local brand images: `custom_components/flicbutton/brand/icon.png`
    (256×256) and `icon@2x.png` (512×512); optional `logo.png` and dark
    variants. **Must not use Flic's trademarked logo.** Claude designs an
    original, generic round-button icon. The owner approves it before the
    first release, and CI may use a placeholder until then.
  - GitHub Actions: `hassfest`, HACS/Marketplace validation action, pytest,
    lint (ruff + black)
  - tagged GitHub releases; a README that documents setup, pairing, the
    `flic_click` event, the flicd requirements, and migration from core
    `flic`
  - repo has description and topics, issues enabled, not archived
- **Client library:** bundled inside the integration
  (`custom_components/flicbutton/flicd/`), **based on the upstream CC0
  `clientlib/python/aioflic.py`** (asyncio `Protocol`). It is adapted to
  allow reconnect, add type hints, and expose an awaitable API (e.g.
  `await client.get_info()`, `await client.ping()`). Keep a CC0 attribution
  note. No external PyPI dependency.
- **Protocol reference:** upstream `ProtocolDocumentation.md`
  (length-prefixed little-endian binary packets over TCP).
- **Click mapping:** `ButtonClick` and `ButtonSingleClick` → `single`,
  `ButtonDoubleClick` → `double`, `ButtonHold` → `hold`. As in legacy, each
  channel subscribes to exactly one flicd click stream, chosen from the
  ignored click types: `double` ignored → ClickOrHold (singles fire at once,
  a fast double press yields two singles); else `hold` ignored →
  SingleOrDoubleClick (a hold arrives as `single`); otherwise
  SingleOrDoubleClickOrHold; all three ignored → no click stream. The ignore
  filter below still applies on top. Since only one stream is subscribed,
  there are no duplicate singles. `ButtonDown`/`ButtonUp` drive the binary
  sensor.
- **Filtering (Claude's call):** filtering happens once, in the client
  callback layer, before anything else sees the click.
  - Ignored click types are suppressed everywhere: `flic_click`, the event
    entity (its `event_types` leave out ignored types), and device triggers.
    Device triggers are built on the event entity.
  - The queued-event timeout drops stale events everywhere, including
    up/down on the binary sensor (legacy did the same).
  - The binary sensor is not affected by ignored click types (as in legacy).
- **Owner's global rules:** metric units and 24 h time only. All internal
  timestamps are timezone-aware UTC. flicd's battery `timestamp` (UNIX
  seconds) is converted with `datetime.fromtimestamp(ts, tz=UTC)`. Never
  use naive datetimes. Run `black` on all Python files.
- **Tests:** `pytest-homeassistant-custom-component`. A fake flicd TCP
  server fixture speaks the real packet format. Coverage targets: config
  flow and options flow 100 %, overall ≥ 90 %. Scenarios that must be
  tested: reconnect after server drop, ping timeout, unavailable/available
  transitions, pairing results, device removal, `flic_click` payload
  equality with legacy.

## 6. Decided by the owner

- Flic 1 is the owner's hardware. The integration also supports Flic 2
  because flicd supports it over the same protocol.
- Built as a custom integration, published in the Marketplace main catalog.
- Separate domain (`flicbutton`), not overriding core `flic`.
- Pairing on demand. Continuous scanning is an option, off by default.
- Integration first; flicd add-on second, and only if an existing one
  cannot be reused (§8).
- GitHub account `frawau`.
- Deleting a button device in HA unpairs it from flicd (`CmdDeleteButton`).
  This is intentional, even though it affects other flicd clients.
- Claude designs the brand icon; the owner approves it.
- Multiple flicd servers for range: yes, one config entry per server, and a
  button belongs to one server only. No roaming.

## 7. Calls made by Claude (owner to check)

- Keep the pressed/released binary sensor alongside the event entity.
- Fixed `button_name` in `flic_click`, independent of renames.
- Backoff 1 → 60 s; ping every 30 s, 10 s timeout.
- Ignored click types and the queue timeout filter all outputs (see §5).
- A button that is re-paired from server A to server B is moved: its device
  (identified by Bluetooth address) moves to B's config entry, and A's entry
  drops it. Automations that target the device keep working.
- Hub device with connectivity + controller-state entities.
- Battery sensor (new; not in legacy).
- Bundle and adapt upstream `aioflic.py` rather than depend on `pyflic`.
- MIT license; minimum HA 2026.11.

## 8. Phase 2 (separate design later): flicd inside HA

Wanted by the owner: when setting up the integration, choose "run flicd
inside Home Assistant", pick the Bluetooth adapter, and refuse if that
adapter is already in use (e.g. by HA's own Bluetooth integration). flicd
takes the adapter exclusively through HCI_CHANNEL_USER.

Facts already known:
- This needs a Supervisor **add-on** (HA OS / Supervised only). It cannot
  be done by the integration itself, and add-ons are not distributed via
  the Marketplace.
- Upstream ships flicd binaries for x86_64, aarch64, armv6l, i386.
- Candidates to reuse first: needs-coffee/flicd-docker (plain Docker), and a
  historic pschmitt add-on repository (status unknown).
- The integration's config flow would gain a first step "existing flicd
  server / install the add-on", in the style of the Matter and Z-Wave JS
  integrations.

Phase 1 must not block this. Keep the connection settings (host/port) as
the single source of truth, so an add-on simply yields host/port.

## 9. Left to the builder

- Internal module layout, coordinator vs. plain runtime-data class, and
  naming of internal classes.
- How the pairing action is exposed in the UI (options-flow menu step with
  `async_show_progress`, or a config subentry flow), as long as the user
  sees progress and the result and it works from the integration page.
- Exact entity naming / translation keys, icons (`icons.json`).
- Whether the event entity and binary sensor are enabled by default
  (recommend: all enabled).
- CI details, release automation, pre-commit setup.
