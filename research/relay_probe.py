#!/usr/bin/env python3
"""BLE relay diagnostics.

Usage:
  .venv/bin/python research/relay_probe.py --notify      # subscribe first, then send frame
  .venv/bin/python research/relay_probe.py --wtype command --chunk 20
"""
import argparse
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

sys.path.insert(0, "/home/scharanruka/dev/agentic/rea/qubo10a-plug-firmware")

ap = argparse.ArgumentParser()
ap.add_argument("--address", default="3C:0F:02:27:CA:4A")
ap.add_argument("--wtype", default="request", choices=("request", "command"))
ap.add_argument("--payload", default="frame", choices=("frame", "garbage"))
ap.add_argument("--chunk", type=int, default=20)
ap.add_argument("--notify", action="store_true")
args = ap.parse_args()

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
from qubo_poller import Plug, STATE_UUID, BLE_KEY, build_relay_payload  # noqa: E402

DD03 = "0000dd03-0000-1000-8000-00805f9b34fb"
p = Plug(args.address)
p.connect()
print("dd02 before :", p.read(STATE_UUID).strip())

seen = []


def handler(iface, changed, _invalid, path=None):
    if "Value" in changed:
        seen.append((path.rsplit("/", 1)[-1], bytes(changed["Value"])))


if args.notify:
    for uuid in (STATE_UUID, DD03):
        p.iface[uuid].StartNotify()
        p.bus.add_signal_receiver(handler, dbus_interface="org.freedesktop.DBus.Properties",
                                  signal_name="PropertiesChanged", path=p.chars[uuid])
    time.sleep(0.5)

data = b"HELLO" if args.payload == "garbage" else build_relay_payload("off", BLE_KEY)
opts = {"type": dbus.String(args.wtype)}
step = args.chunk or len(data)
for i in range(0, len(data), step):
    p.iface[STATE_UUID].WriteValue([dbus.Byte(b) for b in data[i:i + step]], opts)
    time.sleep(0.02)

if args.notify:
    loop = GLib.MainLoop()

    def quit_(*_):
        loop.quit()
        return False
    GLib.timeout_add(3000, quit_)
    loop.run()
    print("notifications :", seen or "none")
else:
    time.sleep(1)

print("dd02 after  :", p.read(STATE_UUID).strip())
print("dd03 after  :", p.read(DD03).strip())
p.disconnect()
