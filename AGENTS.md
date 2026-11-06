# AGENTS.md

Working notes for this repo. The protocol itself is in `FINDINGS.md`; usage is in `README.md`. This file
only covers the environment and the traps that cost time.

## Pipeline

`qubo_poller.py` owns the BLE adapter, polls the plug, integrates energy and publishes MQTT. Home
Assistant consumes that. There is no InfluxDB, no Grafana and no vendor cloud in the path. The kWh sensor
is `total_increasing`, which the Energy Dashboard needs.

## Environment

- BlueZ 5.87 on Arch, but nothing here is Arch-specific beyond the Python packaging.
- `python-dbus` and `python-gobject` come from the distro and are built for the distro Python.
- `bleak` is not installed and not needed. Use D-Bus.
- There is no passwordless sudo, so `btmon` cannot bind its channel. Use D-Bus for anything diagnostic.
- The venv must be created with the distro interpreter and system packages, or `import dbus` fails:

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python paho-mqtt cryptography
```

## Diagnostics

```bash
.venv/bin/python qubo_poller.py --stdout          # print JSON, no MQTT
systemctl --user restart qubo-poller
journalctl --user -u qubo-poller -f
mosquitto_sub -t 'qubo/#' -v -C 5 -W 10
```

`bluetoothctl scan on` prints nothing in non-interactive mode. Use `bluetoothctl --timeout N scan on`
followed by `bluetoothctl devices`.

## Traps

- The plug does not push notifications. Notifications can be enabled, but only about 0-1 event arrives
  per 30 s. Poll instead; one read per second is fine.
- Writes to `dd02` and `ee01` are acknowledged at the ATT layer even when the plug ignores them. A wrong
  key looks exactly like "writes silently ignored".
- Long frames must be chunked. A single large write turns into a BlueZ long write and is never parsed.
- A relay command may drop the link for a few seconds. Do not read `dd02` straight after the write.
- A stale bond after the plug reboots looks like connect-then-drop in under a second, forever. The plug
  still advertises. Fix with `bluetoothctl remove <MAC>`. Bonding is not needed for anything.
- With no reachable Wi-Fi the plug drops the link every ~30 s and reconnects in ~5 s. Relay control still
  works.
- MQTT availability must be republished on every reconnect. A retained `offline` from the last disconnect
  otherwise makes every HA entity show unavailable while the poller runs fine. Publish discovery and
  `online` from the client's `on_connect`.
- HA has removed the YAML `mqtt:` broker schema; the broker is a config entry. Discovery topics must be
  `homeassistant/<component>/<node_id>/<object_id>/config`. A topic like `qubo/<mac>/power/config` is
  silently ignored.
- A stale retained discovery topic reusing a `unique_id` blocks the new entity. Clear it with
  `mosquitto_pub -t '<topic>' -r -n` and restart the poller.
- A numeric MQTT sensor template must not return the string `'unknown'`. Return a JSON `null` instead.

## Editing rules

- Keep the poller dependency-light: `dbus`, `GObject`, `paho-mqtt`, `cryptography`.
- Never trust the plug's `f4`/`f5` counters for kWh; integrate from power and skip gaps longer than three
  poll intervals.
- Filter telemetry per field. Do not discard a whole frame because one field glitched.
- Keep the relay frame in `build_relay_payload()` and the chunking in `Plug.write()`. They are app-exact
  and fragile.
