#!/usr/bin/env python3
"""Try candidate plaintexts for the dd02 relay command and report which flips dd02."""
import sys
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, "/home/scharanruka/dev/agentic/rea/qubo10a-plug-firmware")
from qubo_poller import Plug, STATE_UUID, BLE_KEY  # noqa: E402


def enc(plain, key=BLE_KEY):
    b = plain.encode()
    if len(b) % 16:
        b += b" " * (16 - len(b) % 16)
    e = Cipher(algorithms.AES(key.encode()), modes.ECB()).encryptor()
    return (e.update(b) + e.finalize()).hex()


def wrap(plain, key=BLE_KEY):
    import json
    return json.dumps({"data": enc(plain, key)}, separators=(",", ":")).encode()


svc = '{"devices":{"services":{"lcSwitchControl":{"instanceId":0,"attributes":{"power":"off"}}}}}'
cands = {
    "A short+command, lower on/off": '{"command":' + svc + '}',
    "B short+command, UPPER OFF": '{"command":{"devices":{"services":{"lcSwitchControl":{"instanceId":0,"attributes":{"power":"OFF"}}}}}}',
    "C cloud shape (handleName+uuid)": '{"command":{"devices":{"handleName":"Qubo","deviceUUID":"3C0F0227CA48","services":{"lcSwitchControl":{"instanceId":0,"attributes":{"power":"off"}}}}}}',
    "D no command wrapper": svc,
    "E switchControl instead of lc": '{"command":{"devices":{"services":{"switchControl":{"instanceId":0,"attributes":{"power":"off"}}}}}}',
}

p = Plug("3C:0F:02:27:CA:4A")
p.connect()
base = p.read(STATE_UUID).strip()
print("dd02 baseline:", base)
for name, plain in cands.items():
    before = p.read(STATE_UUID).strip()
    frame = wrap(plain)
    p.write(STATE_UUID, frame, 20)
    time.sleep(1.0)
    after = p.read(STATE_UUID).strip()
    print(f"{name:34s} len={len(frame):3d}  {before} -> {after}" + ("   *** CHANGED ***" if after != before else ""))

# raw hex, no {"data":…} wrapper
before = p.read(STATE_UUID).strip()
p.write(STATE_UUID, enc('{"command":' + svc + "}").encode(), 20)
time.sleep(1.0)
print(f"{'F bare hex, no wrapper':34s}                {before} -> {p.read(STATE_UUID).strip()}")
p.disconnect()
