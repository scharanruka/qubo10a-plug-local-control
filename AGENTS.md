# AGENTS.md

Reverse-engineering workspace for the **Qubo 10A Wi-Fi + Bluetooth Smart Plug (HSP02, India)**, with the goal of
local control + power metrics in Home Assistant on Arch Linux. `qubo_poller.py` polls the plug over BLE and
feeds Home Assistant; the findings behind it are below.

Pipeline (all local, no Qubo cloud): `qubo_poller.py` (host, owns the BLE adapter) → MQTT → Home Assistant.
That is the whole live path — HA's Energy Dashboard and long-term statistics are native, so no InfluxDB or
Grafana is involved. The kWh sensor is `total_increasing`, which is what the Energy Dashboard needs.

## Device facts (verified against live hardware)

| Fact | Value |
| --- | --- |
| BLE address | `3C:0F:02:27:CA:4A` (public) — per-unit, re-scan after factory reset |
| BLE name | `*QBO_P_3C0F0227CA48` → pattern `*QBO_P_<MAC without colons>` |
| MAC OUI | `3C:0F:02` = **Espressif Inc** → the plug is an ESP32, not a Beken/Tuya SoC |
| Model string (char `dd01`) | `HSP02_01_01_14_SYSTEM-10A` |

## GATT map — this is the whole protocol surface

Service `000000dd-0000-1000-8000-00805f9b34fb` (vendor):

| Char | Flags | Value seen | Meaning |
| --- | --- | --- | --- |
| `dd01` | read | `HSP02_01_01_14_SYSTEM-10A` | model / hw / fw / board id |
| `dd02` | read, write, notify | `1` | relay state (on) |
| `dd03` | read, notify | `S_06` | vendor status code, meaning unknown |
| `dd04` | read, notify | `394.67,251.84,73.53,17,0.02` | **live telemetry CSV** |

Service `000000ee-…`: single write-only char `ee01` — most likely the command channel (not cracked).

Everything is **plain ASCII, unencrypted, no auth, no pairing required to read**.

### `dd04` telemetry CSV — 5 comma-separated fields

```
394.67, 251.84, 73.53, 17, 0.02
  f1      f2     f3    f4   f5
```

- `f1` = **current, milliamps** (high confidence)
- `f2` = **voltage, volts** (high confidence, but intermittently reports `0.57` — see below)
- `f3` = **active power, watts** (high confidence)
- Confidence comes from `f1/1000 * f2 ≈ 1.39 * f3` across ~40 samples, i.e. power factor ≈ 0.72 — a realistic
  SMPS load. If `f1` were VA instead, PF would be ~0.18, which is implausible. **Still uncalibrated** — no
  reference load has been measured yet.
- `f4` = monotonic counter, ~1 count per 4–5 min under a steady ~70 W load. **Unconfirmed.**
- `f5` = energy, quantised to 0.01. **Unconfirmed.**

`f4`/`f5` do not agree with each other, so at most one is plain Wh/kWh: observed deltas were `f4` 14→25 while `f5`
went 0.01→0.03 over the same ~50 min at ~70 W (≈58 Wh actually consumed, but `25` and `30` Wh reported). One is
probably a session counter and the other "energy today". Do not trust either until checked against one known
resistive load (e.g. a 100 W incandescent bulb) for several minutes; at 70 W a true 1 Wh counter must tick every
~51 s.

### Behaviour that will waste your time if you don't know it

- **The plug does not push notifications.** Notifications can be enabled — the `2902` CCCD reads `0x0001` — but
  only ~0–1 `PropertiesChanged` arrive per 30 s, versus telemetry that visibly changes on every read. BlueZ
  `PropertiesChanged` events you do see are a side effect of your own `ReadValue`. Implement "streaming" as
  polling; a ~1 s poll is fine (one ATT round trip per read).
- **A relay write only works once the plug holds the matching key.** `dd02` and `ee01` ACK every write at
  the ATT layer regardless, so a wrong key looks exactly like "writes are silently ignored". The frame is
  known and correct (see §"The control problem"); the gate was that a factory-reset plug keeps its old
  `securityKey` and only replaces it when it is provisioned **and joins Wi-Fi successfully**.
- **`dd03` is the plug's status code**: `S_01` = pairing mode (accepts `ee01`), `S_06` = normal,
  `E_05` = Wi-Fi join failed, `\0\0\0\0` = reset. In `S_01` a valid `ee01` frame drops the BLE link ~3 s
  later (the plug reboots); in `E_05` writes are ignored and the link stays up, so each attempt needs a
  button hold back into `S_01`.
- **`dd04` returns the bare string `0` when the relay is off** — the plug stops reporting telemetry
  entirely (no voltage either). Treat it as "relay off, 0 W", not as a corrupt frame.
- **A relay command may drop the BLE link** for a few seconds while the plug applies it. Don't read
  `dd02` straight after the write; trust the command and let the next cycle reconnect and confirm.
- **With no reachable Wi-Fi the plug drops the BLE link every ~30 s** while it retries, reconnecting in
  ~5 s. Relay control and state still work (the relay does not reset), just with a short gap; `dd03`
  reads `\0\0\0\0` in that state instead of `E_05`. Giving it any 2.4 GHz network it can join
  (`research/provision.py`) removes the churn.
- Bonding (Just Works, no passkey) succeeds and exposes **no new services or characteristics**. Bonded state is
  not what unlocks writes.
- **MQTT availability must be republished on every reconnect.** Publishing `online` once at startup is not
  enough: this box hibernates, paho reconnects on resume, and the retained LWT `offline` then sticks forever —
  **every HA entity shows "unavailable" while the poller is perfectly healthy**. Publish discovery + `online`
  from paho's `on_connect`. Symptom to recognise: `mosquitto_sub -t 'qubo/<mac>/availability'` says `offline`
  but `systemctl --user is-active qubo-poller` says `active`.
- **A stale bond looks like "connected then instantly disconnected".** If the plug reboots and forgets its
  key while BlueZ still holds the old one, every connect drops within ~1 s and `ServicesResolved` never goes
  true — so `dd04 not found` fires and the poller loops forever (which the desktop shows as endless
  connect/disconnect notifications). The plug still advertises normally, which is the tell. Fix:
  `bluetoothctl remove <mac>`, then `bluetoothctl --timeout 5 scan on`, then `bluetoothctl pair <mac>`
  (Just Works, ~2 s; GATT tree and telemetry come back immediately). **Bonding is not needed for
  anything** — provisioning reboots the plug and leaves the bond stale, so after provisioning run
  `bluetoothctl remove <mac>` and just connect (measured: 0 reconnect errors in 5 min unbonded).
- **The voltage field drops out.** `f2` intermittently reports `0.57` instead of ~250 for a frame or two (185
  occurrences in 22 h, worse during long uptimes). Do **not** discard the whole frame for it — that silently
  kills current/power/energy too. The poller carries the last good value for `VOLT_CARRY_S` (60 s) and then
  publishes `'unknown'`, which is a real HA state.
- Values also glitch in other fields occasionally, e.g. one frame reading `396.02,0.57,71.31,…`. Filter per
  field; never derive one field from another.

### Minimal working reader (proven, no extra packages)

`python-dbus` + GObject are already installed; `bleak` is **not**. There is no passwordless sudo, so `btmon`
cannot bind its channel — use D-Bus, not HCI dumps.

```python
import dbus
DEV = "/org/bluez/hci0/dev_3C_0F_02_27_CA_4A"
bus = dbus.SystemBus()
dev = bus.get_object("org.bluez", DEV)
dbus.Interface(dev, "org.bluez.Device1").Connect()
ch = dbus.Interface(bus.get_object("org.bluez", DEV + "/service000d/char0013"), "org.bluez.GattCharacteristic1")
print(bytes(ch.ReadValue({})))          # b'394.67,251.84,73.53,17,0.02'   (dd04)
```

Characteristic object paths (`char0013` etc.) are **not stable across reboot/pairing** — resolve them by UUID via
`GetManagedObjects()` instead of hardcoding. Note `Device1` property access needs the
`org.freedesktop.DBus.Properties` interface, not the `org.bluez.Device1` proxy.

## Running it

```bash
# first time: the system dbus module is built for python 3.14; uv's own 3.13 cannot import it
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python paho-mqtt

.venv/bin/python qubo_poller.py --stdout        # debug: print JSON, no MQTT
systemctl --user restart qubo-poller            # the real one
journalctl --user -u qubo-poller -f
podman exec mosquitto mosquitto_sub -t 'qubo/#' -v -C 5 -W 10
```

Both `--system-site-packages` and `--python /usr/bin/python3` are required. uv defaults to its managed 3.13,
which silently breaks `import dbus` (installed for 3.14).

Energy is integrated from `power` inside the poller and persisted to
`~/.local/state/qubo-poller/energy.json` — deliberately **not** read from the plug's `f4`/`f5`. Gaps longer
than 3x the poll interval are skipped so downtime cannot inflate the total.

Services live in `~/containers` (podman-compose, one folder each): `mosquitto` :1883 and `homeassistant` :8123
are the only ones needed. Two containers total; there is deliberately no InfluxDB or Grafana.

### Integration gotchas (verified the hard way)

- HA **removed the `mqtt:` YAML schema** — `broker`/`port` are rejected. The broker is a config entry. Create
  it headless: `POST /api/config/config_entries/flow {"handler":"mqtt"}`, then submit the returned `flow_id`
  with `broker`/`port`/`protocol` **and** `other_settings` (`set_client_cert`, `set_ca_cert`, `transport`).
  Omitting `other_settings` gives `400 required key not provided`.
- HA discovery topics must be `homeassistant/<component>/<node_id>/<object_id>/config`. A plausible-looking
  `qubo/<mac>/power/config` is silently ignored and no entity ever appears.

## The control problem — SOLVED (relay control over BLE)

Local telemetry **and** local relay control now work, with **no cloud account**. The stock BLE command
frame is cracked (from the `com.hero.iot` APK, decompiled with REA/JADX). Summary:

**Relay command** (write to `dd02` in service `000000dd`):
```
payload = {"data":"<hex>"}                       # compact JSON, no space after the colon
  <hex> = lowercase hex of AES-128-ECB-NoPadding(key, plaintext space-padded to 16)
  plaintext = {"command":{"devices":{"services":{"lcSwitchControl":{"instanceId":0,
              "attributes":{"power":"on|off"}}}}}}      # no handleName, no deviceUUID
  key = the plug's securityKey (AES-128, raw UTF-8 bytes) — here: "ABCDEFGHIJKLMNOP"
write = dd02, in chunks of the negotiated MTU (20 B default), each write-with-response
```
- The write char is `dd02` (`WIFI_BULB_COLOR_SERVICE_READ`), **not** `ee01` (`ee01` is commissioning).
- Get the details right: the BLE template has **no** `handleName`/`deviceUUID`, hex is **lowercase**
  (`k3.c.c` → `d(bytes,true)`), and long frames must be **chunked** — a single 300+ byte write is
  turned into a BlueZ long write and the plug never sees a complete frame.

**Provisioning our own key** (the step that actually unlocked control, `research/provision.py`):
```
plaintext JSON → ee01   # NOT encrypted; only `sk` is
  sk = hex( AES-128-ECB("NdRgUkXp2s5vByAD", key padded to 16) )
  a=1, s=<2.4 GHz SSID>, p=<password>, lduid/guid/duid/euid/uuid=<uuid4>,
  spid=d10e4bfb0153496e8e8bb955f7ebe413,
  murl=mqtt.platform.quboworld.com:8883, hurl=srvc.platform.quboworld.com:443,
  ourl="", ota-token="", atok="", rtok="", rc=0
```
Procedure: hold the power button ~10 s until the LED blinks fast (`dd03` = `S_01`) → write the frame →
the plug reboots and joins Wi-Fi → the key is committed. Empty `atok`/`rtok` mean the plug cannot reach
the Qubo cloud (it then reports `E_05`), which is harmless: BLE control keeps working and nothing is
registered anywhere. **Only a successful Wi-Fi join commits the key** — with a network the plug cannot
join it returns to `S_01` and the key is not stored.


Key UUID map (from `AppConstants`):
| const | UUID | meaning |
| --- | --- | --- |
| `WIFI_BULB_READ_SERVICE` | `000000dd` | service (telemetry + relay) |
| `WIFI_BULB_SW_VER_SERVICE_READ` | `dd01` | model string |
| `WIFI_BULB_COLOR_SERVICE_READ` | `dd02` | **relay state + command write** |
| `WIFI_BULB_STATE_SERVICE_READ` | `dd03` | `S_06` status |
| `WIFI_PLUG_POWER_CONSUMPTION_SERVICE_READ` | `dd04` | telemetry CSV |
| `WIFI_BULB_CUSTOM_SERVICE` | `000000ee` | commissioning service |
| `WIFI_BULB_CUSTOM_SERVICE_WRITE` | `ee01` | commissioning write (setup + `sk`) |

Remaining routes (not needed for relay, kept for reference):
1. **ESPHome flash.** `github.com/mdsalim1039/Qubo-10A-Indian-Smart-Plug-Local-Home-Assistant-Integration-ESPHome-`
   (ESP32-C3, BL0937 metering, board stamp `PTTY_OEM_003_01`, relay GPIO4, LED GPIO6, button GPIO10,
   BL0937 SEL GPIO5 / CF GPIO7 / CF1 GPIO3). **Single unverified README created 2026-09-09** — verify
   continuity with a multimeter before powering anything.
2. **Cloud.** `qubopy` (PyPI) exposes switch + power + voltage + current + kWh via Qubo REST/MQTT.

### ⚠️ Mains safety — this overrides everything above

The board is **non-isolated**: DC ground sits at mains potential. Never connect a USB-UART adapter while the
plug is in a wall socket, and never power it from anything but the adapter's isolated 3.3 V. Flashing is
UART-only (GPIO9 to GND for bootloader) — the plug must be pried open.

## Environment gotchas

- BlueZ **5.87** on Arch; `bluetoothctl scan on` prints nothing in non-interactive mode. Use
  `bluetoothctl --timeout N scan on` then `bluetoothctl devices`, or poll D-Bus `Device1`.
- The adapter advertises only `0x1811` (Alert Notification) plus the vendor services. Nothing to learn from the
  advertisement; connect to see the real GATT tree.
