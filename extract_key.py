#!/usr/bin/env python3
"""One-time: read the plug's per-device BLE key (securityKey) from the Qubo cloud.

The plug's relay command is AES-128-ECB encrypted with a key provisioned during
commissioning and stored in the device's cloud attributes. This script logs into
your Qubo account and prints that key so the poller can control the relay locally
(no cloud afterwards). Run once, then pass the key as `--ble-key`.

Usage:  .venv/bin/python extract_key.py
"""
import asyncio
import getpass
import json
import sys

from qubopy import QuboAuth, QuboClient


def _deep_find(obj, key_lower):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == key_lower and isinstance(v, (str, int)):
                return str(v)
            r = _deep_find(v, key_lower)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, key_lower)
            if r:
                return r
    return None


async def main():
    email = input("Qubo email: ").strip()
    password = getpass.getpass("Qubo password: ")
    auth = QuboAuth()
    await auth.login_with_email(email, password)

    async with QuboClient(auth) as client:
        devices = await client.get_all_devices()
        plugs = [d for d in devices if "HSP" in (d.model or "").upper()]
        target = plugs[0] if plugs else (devices[0] if devices else None)
        if target is None:
            print("No devices found on this account.")
            return 1

        print(f"Device: {target.name} [{target.model}] {target.device_id}")
        did = target.device_id

        # Targeted attribute reads. Service name is `bleHub` in the app's model; the
        # bare `securityKey` service is a guess kept as a fallback.
        for service in ("bleHub", "securityKey"):
            try:
                v2 = await client.get_device_shadow_v2(did, service, "securityKey")
            except Exception as e:
                print(f"targeted shadow ({service}) failed: {e}")
                continue
            key = _deep_find(v2, "securitykey")
            if key:
                print(f"SECURITY_KEY={key}")
                return 0
            print(f"no securityKey via service={service}; raw: {json.dumps(v2)[:1500]}")

        shadow = await client.get_device_shadow(did)
        key = _deep_find(shadow.raw, "securitykey")
        if key:
            print(f"SECURITY_KEY={key}")
            return 0

        print("securityKey not found; raw shadow:")
        print(json.dumps(shadow.raw or shadow.reported or {}, indent=2)[:6000])
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
