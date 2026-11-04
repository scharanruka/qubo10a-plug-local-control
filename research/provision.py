#!/usr/bin/env python3
"""Send the BLE commissioning frame (ee01) with a securityKey we choose.

Recovered from BleApplianceCommissionActivity.za(): the app builds a plaintext JSON
(only `sk` is encrypted) and writes it chunked to WIFI_BULB_CUSTOM_SERVICE_WRITE (ee01):

    sk = lowercase hex( AES-128-ECB("NdRgUkXp2s5vByAD", securityKey padded to 16) )

Afterwards the plug should store `securityKey` and accept relay frames on dd02 with it.

Usage:
  .venv/bin/python research/provision.py --dry-run
  .venv/bin/python research/provision.py --ssid MyWiFi --password secret
  .venv/bin/python research/provision.py --ssid MyWiFi --password secret --key ABCDEFGHIJKLMNOP
"""
import argparse
import json
import sys
import time
import uuid as uuidlib

sys.path.insert(0, "/home/scharanruka/dev/agentic/rea/qubo10a-plug-firmware")
from qubo_poller import Plug, STATE_UUID, BLE_KEY, build_relay_payload  # noqa: E402

KDF_KEY = "NdRgUkXp2s5vByAD"      # AppConstants/za(): static key that encrypts `sk`
EE01 = "0000ee01-0000-1000-8000-00805f9b34fb"
DD03 = "0000dd03-0000-1000-8000-00805f9b34fb"
SPID = "d10e4bfb0153496e8e8bb955f7ebe413"


def enc_sk(key):
    """Ab.a.b("NdRgUkXp2s5vByAD", key): AES-128-ECB, space-pad, lowercase hex."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    b = key.encode()
    if len(b) % 16:
        b += b" " * (16 - len(b) % 16)
    e = Cipher(algorithms.AES(KDF_KEY.encode()), modes.ECB()).encryptor()
    return (e.update(b) + e.finalize()).hex()


def build_frame(ssid, password, key):
    u = lambda: str(uuidlib.uuid4())
    return json.dumps({
        "sk": enc_sk(key),
        "a": 1,
        "s": ssid,
        "p": password,
        "lduid": u(), "guid": u(), "duid": u(), "euid": u(),
        "spid": SPID, "uuid": u(),
        # c6.a: murl = MQTT broker, hurl = HTTPS API (values read from the APK's <clinit>)
        "murl": "mqtt.platform.quboworld.com:8883",
        "hurl": "srvc.platform.quboworld.com:443",
        "ourl": "",
        "ota-token": "",
        "atok": "", "rtok": "", "rc": 0,
    }, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default="3C:0F:02:27:CA:4A")
    ap.add_argument("--ssid", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--key", default=BLE_KEY, help="securityKey to install on the plug")
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    frame = build_frame(args.ssid, args.password, args.key)
    print(f"frame ({len(frame)} B): {frame}")
    if args.dry_run:
        return 0

    p = Plug(args.address)
    p.connect()
    print("dd02 before :", p.read(STATE_UUID).strip())
    print("dd03 before :", p.read(DD03))
    p.write(EE01, frame.encode(), args.chunk)
    time.sleep(3)
    print("dd03 after  :", p.read(DD03))

    # now try the relay with the key we just installed
    before = p.read(STATE_UUID).strip()
    p.write(STATE_UUID, build_relay_payload("off", args.key), args.chunk)
    time.sleep(1)
    after = p.read(STATE_UUID).strip()
    print(f"relay dd02  : {before} -> {after}" + ("   *** WORKS ***" if after != before else ""))
    p.disconnect()
    return 0 if after != before else 1


if __name__ == "__main__":
    sys.exit(main())
