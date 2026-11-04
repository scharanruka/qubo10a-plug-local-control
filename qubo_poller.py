#!/usr/bin/env python3
"""Poll a Qubo HSP02 smart plug over BLE and publish to Home Assistant via MQTT.

The plug does not push notifications (its CCCD accepts 0x0001 but almost nothing
arrives), so this polls `dd04` and integrates power into a kWh total itself:
the plug's own energy fields (f4/f5) disagree with each other and are unverified.
"""

import argparse
import json
import os
import queue
import signal
import sys
import time

import dbus
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PLUG_UUID = "0000dd04-0000-1000-8000-00805f9b34fb"
STATE_UUID = "0000dd02-0000-1000-8000-00805f9b34fb"   # relay state (read) AND command (write)
BLE_KEY = "ABCDEFGHIJKLMNOP"   # AppConstants.BLE_KEY; per-device securityKey overrides this
STATE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "qubo-poller", "energy.json")

# sanity bounds; the plug occasionally emits a corrupt field (e.g. voltage 0.57)
V_MIN, V_MAX = 80.0, 300.0
P_MIN, P_MAX = 0.0, 4000.0
VOLT_CARRY_S = 60          # how long a stale-but-good voltage is reused during a glitch


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def build_relay_payload(power, key):
    """Encode a relay on/off command exactly as the app builds it for the dd02 write char.

    Ab.n0.A: inner JSON from R.string.control_bt_* — the BLE template has no handleName
    and no deviceUUID (those are cloud-only). Ab.a: AES-128-ECB/NoPadding over the JSON
    space-padded to 16 bytes. k3.c.c: *lowercase* hex. Then wrapped as {"data": "<hex>"}.
    """
    cmd = {"command": {"devices": {"services": {"lcSwitchControl": {
        "instanceId": 0, "attributes": {"power": power}}}}}}
    plain = json.dumps(cmd, separators=(",", ":")).encode()
    r = len(plain) % 16
    if r:
        plain += b" " * (16 - r)
    enc = Cipher(algorithms.AES(key.encode()), modes.ECB()).encryptor()
    cipher = enc.update(plain) + enc.finalize()
    return json.dumps({"data": cipher.hex()}, separators=(",", ":")).encode()


def load_energy():
    try:
        with open(STATE_FILE) as f:
            return float(json.load(f)["kwh"])
    except (OSError, ValueError, KeyError):
        return 0.0


def save_energy(kwh):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"kwh": round(kwh, 6)}, f)
    os.replace(tmp, STATE_FILE)


class Plug:
    """Thin BlueZ D-Bus client. Object paths change on reconnect, so resolve by UUID."""

    def __init__(self, address):
        self.address = address
        self.dev_path = None
        self.iface = None
        self.chars = {}
        self.last_volts = None
        self.bad_volts_since = None
        self.bus = dbus.SystemBus()

    def _om(self):
        return dbus.Interface(self.bus.get_object("org.bluez", "/"),
                              "org.freedesktop.DBus.ObjectManager")

    def _adapter(self):
        for path, ifaces in self._om().GetManagedObjects().items():
            if "org.bluez.Adapter1" in ifaces:
                return dbus.Interface(self.bus.get_object("org.bluez", path),
                                      "org.bluez.Adapter1")
        return None

    def _resolve(self):
        chars = {}
        dev_path = None
        for path, ifaces in self._om().GetManagedObjects().items():
            if "org.bluez.Device1" in ifaces:
                if ifaces["org.bluez.Device1"].get("Address") == self.address:
                    dev_path = path
            elif "org.bluez.GattCharacteristic1" in ifaces:
                uuid = str(ifaces["org.bluez.GattCharacteristic1"]["UUID"])
                chars[uuid] = path
        self.dev_path = dev_path
        self.chars = chars

    def _props(self):
        return dbus.Interface(self.bus.get_object("org.bluez", self.dev_path),
                              "org.freedesktop.DBus.Properties")

    def _bonded(self):
        try:
            return bool(self._props().Get("org.bluez.Device1", "Bonded"))
        except Exception:
            return False

    def _discover(self, timeout=20):
        # The plug advertises continuously; scan until BlueZ knows it. Needed
        # after a reboot (empty device cache) or after RemoveDevice.
        adapter = self._adapter()
        if adapter is None:
            return
        try:
            adapter.StartDiscovery()
        except Exception:
            return
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and self.dev_path is None:
                time.sleep(0.5)
                self._resolve()
        finally:
            try:
                adapter.StopDiscovery()
            except Exception:
                pass

    def _clear_stale_bond(self):
        # We never need to bond (reads work unencrypted). A leftover bond goes
        # stale when the plug reboots and regenerates its key, after which every
        # connect fails with an auth error. Drop it and rediscover the plug.
        adapter = self._adapter()
        if adapter is None:
            return
        try:
            adapter.RemoveDevice(self.dev_path)
        except Exception:
            pass
        self.dev_path = None
        self._discover()

    def connect(self):
        self._resolve()
        if not self.dev_path:
            self._discover()
        if not self.dev_path:
            raise RuntimeError(f"{self.address} not known to BlueZ; is it in range?")
        if self._bonded():
            self._clear_stale_bond()
        if not self.dev_path:
            raise RuntimeError(f"{self.address} not rediscovered after clearing stale bond")
        dev = dbus.Interface(self.bus.get_object("org.bluez", self.dev_path),
                             "org.bluez.Device1")
        props = self._props()
        if not props.Get("org.bluez.Device1", "Connected"):
            dev.Connect()
        for _ in range(100):                       # wait for GATT discovery
            if props.Get("org.bluez.Device1", "ServicesResolved"):
                break
            time.sleep(0.1)
        self._resolve()
        self.iface = {
            uuid: dbus.Interface(self.bus.get_object("org.bluez", path),
                                 "org.bluez.GattCharacteristic1")
            for uuid, path in self.chars.items()
        }
        if PLUG_UUID not in self.iface:
            raise RuntimeError("telemetry characteristic dd04 not found")

    def read(self, uuid):
        return bytes(self.iface[uuid].ReadValue({}, timeout=10)).decode("ascii", "replace")

    def write(self, uuid, data, chunk=20):
        """Write `data` to a characteristic the way the app does: sequentially, in
        `chunk`-byte pieces, each write-with-response (f3.d.j splits at the MTU; the
        next chunk is triggered by the previous write's callback).

        chunk=0 sends the whole payload in one write.
        """
        if chunk <= 0 or len(data) <= chunk:
            self.iface[uuid].WriteValue([dbus.Byte(b) for b in data], {})
            return
        for i in range(0, len(data), chunk):
            self.iface[uuid].WriteValue(
                [dbus.Byte(b) for b in data[i:i + chunk]], {})
            time.sleep(0.02)             # app waits for the write callback before the next chunk

    def disconnect(self):
        try:
            dbus.Interface(self.bus.get_object("org.bluez", self.dev_path),
                           "org.bluez.Device1").Disconnect()
        except Exception:
            pass

    def sample(self):
        """Return dict in SI units with voltage possibly None, or None if the frame is unusable."""
        raw = self.read(PLUG_UUID).strip()
        self.last_raw = raw
        f = raw.split(",")
        if raw == "0":          # relay off: the plug stops reporting telemetry entirely
            f = ["0"] * 5
        if len(f) != 5:
            return None
        try:
            current_ma, volts, watts = float(f[0]), float(f[1]), float(f[2])
        except ValueError:
            return None
        if not (P_MIN <= watts <= P_MAX):
            return None

        out = {"current": round(current_ma / 1000.0, 3), "power": watts}
        now = time.time()
        if V_MIN <= volts <= V_MAX:
            self.last_volts, self.bad_volts_since = volts, None
            out["voltage"] = volts
        else:
            # the plug intermittently reports 0.57 V for a single frame; hold the last good
            # value briefly, then report unknown — current/power must keep flowing regardless
            if self.bad_volts_since is None:
                self.bad_volts_since = now
            carry = now - self.bad_volts_since < VOLT_CARRY_S
            out["voltage"] = self.last_volts if (carry and self.last_volts) else None
        return out


def publish_discovery(mqtt, base, node, device):
    # HA only listens on <prefix>/<component>/<node_id>/<object_id>/config
    sensors = {
        "voltage": ("Voltage", "V", "voltage", "measurement", None, "sensor"),
        "current": ("Current", "A", "current", "measurement", None, "sensor"),
        "power": ("Power", "W", "power", "measurement", None, "sensor"),
        "energy": ("Energy", "kWh", "energy", "total_increasing", "energy", "sensor"),
        "relay": ("Relay", None, "power", None, None, "switch"),
    }
    for key, (name, unit, dclass, sclass, eclass, component) in sensors.items():
        template = "{{ value_json.%s }}" % key
        # a JSON null renders as "None", which HA's MQTT sensor maps to a real unknown state;
        # a literal 'unknown' string is rejected for a numeric sensor and logs an error every time
        cfg = {"name": name,
               "unique_id": f"{node}_{key}",
               "state_topic": f"{base}/state",
               "value_template": template,
               "device": device,
               "availability_topic": f"{base}/availability"}
        if unit:
            cfg["unit_of_measurement"] = unit
        if sclass:
            cfg["state_class"] = sclass
        if eclass:
            cfg["device_class"] = eclass
        elif component == "sensor":
            cfg["device_class"] = dclass
        if key == "relay":
            cfg["command_topic"] = f"{base}/set"
            cfg["payload_on"], cfg["payload_off"] = "ON", "OFF"
            cfg["state_on"], cfg["state_off"] = "1", "0"
            cfg["device_class"] = "switch"
            cfg["icon"] = "mdi:power-socket"
        mqtt.publish(f"homeassistant/{component}/{node}/{key}/config",
                     json.dumps(cfg), retain=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--address", default="3C:0F:02:27:CA:4A")
    ap.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--ble-key", default=BLE_KEY,
                    help="AES-128 key for the BLE command (the plug's securityKey)")
    ap.add_argument("--chunk", type=int, default=20,
                    help="write the frame in N-byte pieces (0 = one write)")
    ap.add_argument("--relay", choices=("on", "off"),
                    help="send one relay command and exit (no MQTT)")
    ap.add_argument("--stdout", action="store_true", help="print JSON instead of MQTT")
    args = ap.parse_args()

    if args.relay:
        plug = Plug(args.address)
        plug.connect()
        before = plug.read(STATE_UUID).strip()
        frame = build_relay_payload(args.relay, args.ble_key)
        log(f"frame ({len(frame)} B, chunk={args.chunk}): {frame.decode()}")
        plug.write(STATE_UUID, frame, args.chunk)
        try:
            time.sleep(1.0)
            after = plug.read(STATE_UUID).strip()
        except Exception:
            after = None            # the plug drops the link right after applying a command
        plug.disconnect()
        log(f"relay state dd02: {before} -> {after or '(link dropped: command applied)'}")
        return 0 if after != before else 1

    mqtt = None
    base = "qubo/" + args.address.replace(":", "").lower()
    node = "qubo_" + args.address.replace(":", "").lower()
    device = {"identifiers": [node], "name": "Qubo Smart Plug",
              "manufacturer": "Hero Electronix", "model": "HSP02 (10A)"}
    pending = queue.Queue()          # relay commands from HA ("ON"/"OFF")
    state_cache = {}                 # last published state, so a command can be acknowledged at once

    if not args.stdout:
        import paho.mqtt.client as mqttlib
        mqtt = mqttlib.Client(mqttlib.CallbackAPIVersion.VERSION2, client_id="qubo-poller")
        if args.mqtt_user:
            mqtt.username_pw_set(args.mqtt_user, args.mqtt_password)
        mqtt.will_set(f"{base}/availability", "offline", retain=True)
        mqtt.reconnect_delay_set(1, 60)

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            # Fires on every (re)connect. Without this the retained LWT 'offline' sticks after a
            # suspend or network blip and every HA entity shows unavailable while we are running.
            if reason_code.is_failure:
                log(f"mqtt connect refused: {reason_code}")
                return
            publish_discovery(mqtt, base, node, device)
            mqtt.subscribe(f"{base}/set")
            mqtt.publish(f"{base}/availability", "online", retain=True)

        def on_message(_client, _userdata, msg):
            cmd = msg.payload.decode().strip().upper()
            if cmd in ("ON", "OFF"):
                pending.put(cmd)
                if state_cache:          # echo the new relay now; the loop confirms it next cycle
                    mqtt.publish(f"{base}/state",
                                 json.dumps({**state_cache,
                                             "relay": "1" if cmd == "ON" else "0"}),
                                 retain=True)

        mqtt.on_connect = on_connect
        mqtt.on_message = on_message
        mqtt.connect(args.mqtt_host, args.mqtt_port, 60)
        mqtt.loop_start()

    energy_kwh = load_energy()
    plug = None
    last_ts = None
    last_save = 0.0
    desired = None                  # relay command still to be delivered (survives reconnects)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        try:
            if plug is None:
                log(f"connecting to {args.address}")
                plug = Plug(args.address)
                plug.connect()
                log("connected")
                last_ts = None                     # don't integrate across a gap

            s = plug.sample()
            if s is None:
                log(f"dropped corrupt telemetry frame: {plug.last_raw!r}")
                time.sleep(args.interval)
                continue

            now = time.time()
            if last_ts is not None:
                dt = now - last_ts
                if dt <= args.interval * 3:        # ignore gaps after a stall
                    energy_kwh += s["power"] * dt / 3600.0 / 1000.0
            last_ts = now
            if now - last_save > 30:               # don't rewrite the file every poll
                save_energy(energy_kwh)
                last_save = now

            commanded = None
            while not pending.empty():             # relay command from HA
                desired = "on" if pending.get() == "ON" else "off"   # latest wins
            if desired is not None:
                try:
                    plug.write(STATE_UUID, build_relay_payload(desired, args.ble_key), args.chunk)
                    commanded = "1" if desired == "on" else "0"
                    log(f"relay -> {desired}")
                    desired = None
                except Exception as e:
                    # the plug blips the BLE link while it retries Wi-Fi, so a command can land in a
                    # gap; keep it and try again next cycle instead of silently dropping it
                    log(f"relay command failed: {e}; will retry")

            # the plug drops the BLE link a moment after applying a relay command, so trust the
            # command we sent instead of reading back (the next cycle reconnects and confirms)
            if commanded is not None:
                s["relay"] = commanded
            elif desired is not None:                 # still undelivered: keep showing what was asked
                s["relay"] = "1" if desired == "on" else "0"
            else:
                s["relay"] = plug.read(STATE_UUID).strip()
            s["energy"] = round(energy_kwh, 4)
            payload = json.dumps(s)
            state_cache = s              # rebind (atomic) so the MQTT thread never sees a half-updated dict
            if mqtt:
                mqtt.publish(f"{base}/state", payload, retain=True)
            else:
                log(payload)
        except Exception as e:
            log(f"error: {e}; reconnecting")
            if plug:
                plug.disconnect()
            plug = None
            last_ts = None
            time.sleep(5)
            continue

        time.sleep(args.interval)

    save_energy(energy_kwh)
    if mqtt:
        mqtt.publish(f"{base}/availability", "offline", retain=True)
        mqtt.loop_stop()
        mqtt.disconnect()
    if plug:
        plug.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
