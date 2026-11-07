# Flic button (flicd)

A Home Assistant integration for [Flic](https://www.flic.io/) buttons, served by
the `flicd` daemon. It connects to one or more `flicd` servers over TCP, turns
every paired button into a Home Assistant device, and fires `flic_click` events.
It reconnects on its own after a network or power loss.

Not affiliated with Shortcut Labs / Flic. Flic is a trademark of its owner.

## Requirements

- `flicd` from [50ButtonsEach/fliclib-linux-hci](https://github.com/50ButtonsEach/fliclib-linux-hci),
  running on a Linux machine (a Raspberry Pi Zero works well).
- When Home Assistant runs on another machine, start `flicd` with `-s 0.0.0.0`
  so it accepts remote clients:

  ```sh
  ./flicd -f flic.sqlite3 -s 0.0.0.0 -p 5551
  ```

- A Bluetooth adapter dedicated to `flicd`. `flicd` takes the adapter
  exclusively, so nothing else on that machine can use it.
- Internet access on the `flicd` host while pairing (the Flic backend verifies
  new buttons).
- Home Assistant 2026.11.0 or newer.

## Install

Install through the Marketplace (Settings > Marketplace), search for
"Flic button (flicd)", download it and restart Home Assistant. Until it is in the
default catalog, use "Add from link" in the Marketplace with
`https://github.com/frawau/flicbutton`.

## Setup

1. Settings > Devices & services > Add integration > "Flic button (flicd)".
2. Enter the host name or IP address of the `flicd` server and its port
   (default 5551). The connection is tested before the entry is created.

Add one entry per `flicd` server. Already paired buttons appear as devices
immediately.

Options (Configure on the entry > Settings):

| Option | Default | Meaning |
| --- | --- | --- |
| Queue timeout | 3 s | Clicks that `flicd` queued for longer than this while Home Assistant was disconnected are dropped. 0 drops all queued clicks. |
| Ignored click types | none | Click types that do not fire events. |
| Automatic pairing | off | Keeps the pairing wizard running, so any button held for 7 s gets paired. |

## Pairing a button

1. Configure on the entry > Pair a new button.
2. Press and hold the Flic button for 7 s until it blinks.
3. The dialog reports the result: paired, nothing found, button in private mode
   (hold it 7 s to make it public and retry), Bluetooth controller unavailable,
   Flic backend unreachable, and so on.

Pairing is refused while automatic pairing is on. Turn automatic pairing off to
pair manually.

Deleting a button device in Home Assistant unpairs it from `flicd`. If `flicd`
removes a button (for example after a factory reset), its device is removed from
Home Assistant.

## Multiple servers for range

A Flic button connects to one `flicd` server at a time. To cover a larger
area, run `flicd` on several machines, add each as an entry, and pair each
button to the server closest to it.

To move a button to another server, pair it to the new server from the new
server's Configure > Pair a new button. Home Assistant then moves the device to
that server automatically, and device-targeted automations keep working.

Limitation: if the button is deleted from its old `flicd` server before it is
paired to the new one, Home Assistant treats it as a new device. Automations
that target the device then need to be re-selected. Pairing directly to the new
server keeps the device and its automations.

## Entities

Per button:

| Entity | Notes |
| --- | --- |
| Click (`event`) | Event types `single`, `double`, `hold`. |
| Pressed (`binary_sensor`) | On while the button is held down. |
| Battery (`sensor`) | Percent, diagnostic. Unknown when `flicd` reports no value. |

Per server (hub device):

| Entity | Notes |
| --- | --- |
| Connectivity (`binary_sensor`) | Diagnostic. Connection to `flicd`. |
| Bluetooth controller (`sensor`) | Diagnostic. `detached`, `resetting` or `attached`. |

While the connection to a server is down, all its entities are unavailable.
Button devices show the serial number, color, Flic version (Flic 1 or Flic 2)
and firmware version.

## Device triggers

In the automation editor, pick a button as the device and choose "Single
click", "Double click" or "Hold". Click types listed in "Ignored
click types" are not offered.

## The `flic_click` event

Each click also fires a `flic_click` event on the Home Assistant event bus, with
the same name and keys as the core `flic` integration:

| Key | Value |
| --- | --- |
| `button_name` | `flic_<mac without colons, lowercase>`, for example `flic_80e4da712b3c`. Fixed. It does not follow renames in the UI. |
| `button_address` | The Bluetooth address, `aa:bb:cc:dd:ee:ff`, lowercase. |
| `click_type` | `single`, `double` or `hold`. |
| `queued_time` | Integer seconds the click was queued in `flicd` before delivery. |

```yaml
automation:
  - alias: "Hallway light on double click"
    triggers:
      - trigger: event
        event_type: flic_click
        event_data:
          button_name: flic_80e4da712b3c
          click_type: double
    actions:
      - action: light.toggle
        target:
          entity_id: light.hallway
```

## Migrating from core `flic`

1. Remove the `binary_sensor:` block with `platform: flic` from your YAML
   configuration and restart Home Assistant.
2. Add "Flic button (flicd)" through the UI, pointing at the same `flicd`.
3. Your existing `flic_click` automations keep working, because the event name
   and payload are unchanged.

The new entities have different entity ids than the legacy binary sensors, so
automations or dashboards that reference those need updating.

## Troubleshooting

- **Cannot connect**: check host and port, that `flicd` runs, and that it was
  started with `-s 0.0.0.0` when it is on another machine. Check firewalls.
- **Pairing fails with "Bluetooth controller unavailable"**: `flicd` has no
  usable adapter. Another service may hold it.
- **Pairing fails with a backend error**: the `flicd` host needs internet access.
- **"Button is private"**: hold the button for 7 s, then pair again.
- **Everything unavailable after a reboot of the `flicd` host**: the integration
  retries with an increasing delay (1 s up to 60 s) and recovers on its own.
  A dead connection is detected within 40 s (ping every 30 s, 10 s answer timeout).
- **Diagnostics**: download them from the entry's menu when reporting an issue.

Report problems at <https://github.com/frawau/flicbutton/issues>.

## License

MIT. See `LICENSE`.
