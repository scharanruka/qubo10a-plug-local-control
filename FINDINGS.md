# Qubo HSP02 — protocol reference

Everything below was recovered from the vendor app (`com.hero.iot`, version 3.3.42) and verified against
a live plug. It is the minimum needed to talk to the plug without the vendor cloud. Steps for using it
are in `README.md`.

The plug is an ESP32 with a plain-ASCII, unauthenticated GATT interface. Reads need no pairing, no
encryption and no account.

## GATT map

Service `000000dd-0000-1000-8000-00805f9b34fb`:

| Characteristic | Flags | Value seen | Meaning |
| --- | --- | --- | --- |
| `dd01` | read | `HSP02_01_01_14_SYSTEM-10A` | model / hardware / firmware / board |
| `dd02` | read, write, notify | `1` | relay state, and the relay command channel |
| `dd03` | read, notify | `S_06` | vendor status code |
| `dd04` | read, notify | `394.67,251.84,73.53,17,0.02` | live telemetry CSV |

Service `000000ee-...` has a single write-only characteristic `ee01`, the commissioning channel.

Object paths such as `char0013` are not stable across reboots or pairings. Resolve characteristics by
UUID through `GetManagedObjects()`.

## Telemetry (`dd04`)

Five comma-separated fields:

```
394.67, 251.84, 73.53, 17, 0.02
  f1      f2     f3    f4   f5
```

- `f1` current in milliamps.
- `f2` voltage in volts.
- `f3` active power in watts.
- `f4` monotonic counter, roughly one count per 4-5 minutes at ~70 W. Purpose unconfirmed.
- `f5` energy, quantised to 0.01. Purpose unconfirmed.

`f1/1000 * f2` is about `1.39 * f3` across samples, implying a power factor near 0.72, which is
plausible for a switch-mode load. Nothing here is calibrated.

`f4` and `f5` disagree with each other and `f4` resets on provisioning, so neither is trustworthy as a
Wh counter. The poller integrates power itself.

The relay being off makes `dd04` return the bare string `0`: no voltage, no current, no frame. Treat it
as "relay off, 0 W", not as corruption.

Values glitch per field. `f2` occasionally reads `0.57` for a frame or two. Filter each field separately
and never derive one from another.

## Status codes (`dd03`)

| Value | Meaning |
| --- | --- |
| `S_01` | pairing mode; accepts `ee01` |
| `S_06` | normal operation |
| `E_05` | Wi-Fi join or cloud auth failed |
| `\0\0\0\0` | no Wi-Fi, or reset state |

In `S_01`, a valid `ee01` frame makes the plug drop the BLE link about 3 s later while it reboots. In
`E_05` writes are ignored and the link stays up, so each attempt needs another button hold back into
`S_01`.

## Relay command

Written to `dd02`. The frame is the app's Bluetooth variant: compact JSON, lowercase hex, no
`handleName` and no `deviceUUID`.

```
plaintext = {"command":{"devices":{"services":{"lcSwitchControl":{"instanceId":0,
             "attributes":{"power":"on"}}}}}}          # "on" or "off", padded with spaces to 16 bytes
hex       = lowercase( AES-128-ECB-NoPadding(securityKey, plaintext) )
frame     = {"data":"<hex>"}                             # no space after the colon
key       = the plug's securityKey, used as raw UTF-8 bytes
```

Write the frame to `dd02` in chunks of the negotiated MTU (20 bytes by default), each write with
response, sequentially and in order. A single long write becomes a BlueZ prepare/execute write and the
plug never sees a complete frame.

Notes:

- The service name is `lcSwitchControl` and the attribute is `power`; the values are `on`/`off`, not
  `1`/`0`.
- The cloud command format is different and includes `handleName` and `deviceUUID`. Do not use it here.
- A relay command may drop the link for a few seconds while the plug applies it. Trust the command and
  confirm on the next read rather than reading `dd02` immediately.

## Provisioning (`ee01`)

The commissioning frame is unencrypted JSON. Only the `sk` field is encrypted, and with a fixed key from
the app rather than the per-device key:

```
sk = hex( AES-128-ECB("NdRgUkXp2s5vByAD", key padded to 16 bytes) )
```

Fields: `a=1`, `s` (SSID), `p` (password), `lduid`, `guid`, `duid`, `euid`, `uuid`, `spid`, `murl`,
`hurl`, `ourl`, `ota-token`, `atok`, `rtok`, `rc=0`. Write it to `ee01` with the same chunked write as
above.

- The plug replaces its stored `securityKey` with the supplied key only after it successfully joins
  Wi-Fi. A failed join returns it to `S_01` with the old key intact.
- Empty `atok`/`rtok` mean the plug cannot authenticate to the vendor cloud. It then reports `E_05`,
  which is harmless: BLE control keeps working and nothing is registered anywhere.
- Cloud hostnames in the app's constants are not needed. Leaving the URL fields at their defaults did
  not change the outcome.

## What did not work

Kept short so these are not retried:

- Bonding. Just Works pairing succeeds and exposes no new services. The stored bond goes stale after the
  plug reboots, which looks like "connects, then drops within a second". Remove the bond and connect
  unbonded; measured 0 reconnect errors in 5 minutes.
- Sending a single long `WriteValue`. The plug's handler ignores it; chunking is required.
- Uppercase hex, or the cloud-shaped JSON with `handleName`/`deviceUUID`. Both are silently ignored.
- Deriving the key locally. `securityKey` is generated by the vendor cloud during commissioning, which is
  why this project installs its own key instead.
- Wi-Fi as a local API. The app has no mDNS, CoAP, SSDP or device-HTTP code. On Wi-Fi the plug only
  speaks MQTT/TLS to the vendor broker, so it changes the transport, not the account gate.
- Factory reset as a way to clear the key. It does not.

## Rebuilding this

The vendor app was decompiled with JADX; the helper scripts in `research/` search DEX string pools and
static field initialisers, which JADX cannot do by text. The app binary is not distributed here. Re-derive
the constants from an APK you obtained legally if you need to confirm them, and treat the values above as
the expected result rather than as a secret.

## Safety

The board is non-isolated: DC ground sits at mains potential. Never attach a USB-UART adapter while the
plug is in a socket, and power it only from an isolated 3.3 V supply. UART flashing needs GPIO9 to ground
for the bootloader and the case pried open. Verify any pin map with a multimeter before applying power.
