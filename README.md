# Qubo 10A smart plug — local BLE control → Home Assistant

Control and meter a **Qubo HSP02 (10A / 16A) Wi‑Fi + Bluetooth smart plug** with no cloud account, no
app, and no internet. A small Python daemon on any Linux box with a Bluetooth adapter talks to the plug
over BLE and bridges it to Home Assistant through MQTT.

Tested on `HSP02_01_01_14_SYSTEM-10A` (ESP32, advertising as `*QBO_P_<MAC without colons>`).

```
qubo_poller.py  ──BLE──►  plug
      │
      └──MQTT──►  Home Assistant (switch + V + A + W + kWh)
```

## What you need

- Linux host with a Bluetooth adapter, within ~10 m of the plug (BlueZ ≥ 5.60).
- `python-dbus` / `python-gobject` from the distro (the poller uses BlueZ over D‑Bus, not `bleak`).
- An MQTT broker and Home Assistant (the poller publishes discovery itself).
- **No Qubo account.** No internet on the plug either, if you want.

## 1. Find the plug's MAC

```bash
bluetoothctl --timeout 15 scan on >/dev/null; bluetoothctl devices | grep QBO_P
```

The name is `*QBO_P_3C0F0227CA48` and the address is the same digits with colons — note it down.

## 2. Set up the Python environment

The D‑Bus module is built for the distro's Python, so the venv must use that interpreter and see system
packages:

```bash
cd qubo10a-plug-firmware
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python paho-mqtt cryptography
```

## 3. Give the plug a key (one time, the only tricky step)

Out of the box the plug holds a `securityKey` from its previous life and **ignores every command** that
isn't encrypted with it (factory reset does not clear it). We install our own key. Nothing leaves your
network — the plug is just told a Wi‑Fi SSID/password and the new key.

1. Hold the plug's power button ~10 s until the LED blinks fast — that is its pairing state (`dd03`
   reads `S_01`). The script refuses to run if the plug is not in it.
2. Run, with any 2.4 GHz network the plug can join (a phone hotspot is fine):

```bash
.venv/bin/python research/provision.py --ssid "<SSID>" --password "<password>"
```

It sends the commissioning frame, the plug reboots to join Wi‑Fi, and then the script tests the relay.
Success looks like:

```
relay dd02  : 1 -> 0   *** WORKS ***
```

If it prints `S_01` again, the Wi‑Fi join failed (wrong band/password, or 2.4 GHz disabled) — fix that and
hold the button again. **The key is only stored after a successful join.**

The default key is `ABCDEFGHIJKLMNOP`; use `--key` to choose another (16 characters) and pass the same
value as `--ble-key` to the poller.

## 4. Run the poller

One-shot check, no MQTT:

```bash
.venv/bin/python qubo_poller.py --stdout
.venv/bin/python qubo_poller.py --relay off     # flip the relay once and exit
```

Service:

```bash
mkdir -p ~/.config/systemd/user
cp qubo-poller.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now qubo-poller
journalctl --user -u qubo-poller -f
```

## 5. Home Assistant

Nothing to configure if the MQTT integration is already set up — the poller publishes retained discovery
for one device with five entities:

| entity | topic |
| --- | --- |
| `switch.qubo_smart_plug_relay` | `qubo/<mac>/set` (`ON`/`OFF`) |
| `sensor.qubo_smart_plug_voltage` / `_current` / `_power` | `qubo/<mac>/state` |
| `sensor.qubo_smart_plug_energy` (kWh, `total_increasing`) | `qubo/<mac>/state` |

## Keeping it off the internet

The stock firmware has **no local Wi‑Fi API** — on Wi‑Fi it only opens an outbound MQTT/TLS connection to
Qubo's cloud. Local control is always BLE. Joining Wi‑Fi is still worth doing: it stops the plug retrying
Wi‑Fi and keeps the BLE link stable.

With the key installed there are no cloud tokens on the plug, so it cannot authenticate anywhere. To be
certain nothing leaves, block its MAC from WAN at the router (or put it on an isolated SSID/VLAN with no
internet) *after* provisioning, and check that `dd03` still reads `E_05` (cloud rejected) rather than
`\0\0\0\0` (Wi‑Fi lost).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Relay never changes, everything else fine | wrong key | re-provision (§3) |
| LED blinks fast, `dd03` = `S_01` | pairing mode, accepts provisioning | expected during §3 |
| `dd03` = `E_05` | Wi‑Fi joined, cloud auth failed | harmless, no cloud account |
| `dd03` = `\0\0\0\0` | no Wi‑Fi | BLE still works, link blips every ~30 s |
| Link drops every ~30 s | plug retrying an absent Wi‑Fi | give it a reachable 2.4 GHz network |
| Reconnects every 1–2 min, link dies right after connect | stale bond after the plug rebooted | `bluetoothctl remove <MAC>` — bonding is not needed |
| Switch missing in HA | stale retained discovery with a duplicate `unique_id` | `mosquitto_pub -t '<old topic>' -r -n`, restart the poller |
| Voltage sensor errors in HA log | template returned `'unknown'` | fixed in this version; keep the JSON `null` |

## Scope and limits

- One poller process per plug; the adapter handles ~7 concurrent BLE links, so 4–6 plugs per dongle.
  `client_id` is currently fixed — duplicate it per instance before running more than one.
- BLE range is the real limit; a second dongle or a host nearer the plugs beats a longer antenna.
- `base.apk` (the vendor app) is **not** in this repo; it is only needed to re-derive the protocol, which
  is documented in `FINDINGS.md`.
- Protocol details, the full decompiled call chain, and the dead ends are in `FINDINGS.md`; operational
  gotchas are in `AGENTS.md`.

## Files

| file | purpose |
| --- | --- |
| `qubo_poller.py` | poller, relay frame, MQTT bridge |
| `research/provision.py` | installs a known `securityKey` over `ee01` |
| `research/relay_matrix.py` | negative matrix that ruled out frame-shape hypotheses |
| `research/relay_probe.py`, `relay_variants.py` | low-level write diagnostics |
| `extract_key.py` | optional: pull the key from the vendor cloud (not needed) |
| `FINDINGS.md`, `AGENTS.md` | research log and hard-won operational notes |
