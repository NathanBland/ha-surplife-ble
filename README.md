# Surplife BLE

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This integration controls Surplife or MagicHome2 Bluetooth Low Energy (BLE) LED lights in Home Assistant.
The lights use the IOTBT EA81 protocol.
These lights include the MIHEAL or Dongguan DaXian DX23012 floor lamp and similar devices.
These devices advertise with the IOTBT local name prefix.

## Features

| Capability | Status | Notes |
| --- | --- | --- |
| Power on/off | Verified working | `E0` command via `FF01` |
| RGB color | Verified working | `3B A1` command via `FF01`, changes physical output |
| White brightness | Verified working | `3B B1` command via `FF01`, updates device state |
| Brightness | Working | Sends the full color/white command again with the new brightness value |
| Multiple devices | Supported | Each device gets its own config entry |
| BLE discovery | Supported | Automatic discovery via `IOTBT` name prefix |
| Manual fallback | Supported | Enter the address manually if discovery does not find the device |

## Supported devices

- MIHEAL or Dongguan DaXian DX23012 floor lamp
- Other IOTBT-protocol BLE lights (advertise with the `IOTBT` local name, for example `IOTBT144`)

## Requirements

- Home Assistant 2024.1 or later
- A Bluetooth adapter (built-in, USB, or an ESPHome Bluetooth proxy)
- A Surplife or MagicHome2 BLE light that uses the IOTBT protocol

## Installation

### Via HACS

1. Add this repository as a custom repository in HACS. Use the Integration type.
2. Install "Surplife BLE" from HACS.
3. Restart Home Assistant.
4. Add the integration through **Settings → Devices and Services → Add Integration**.
5. Select a discovered device from the list. Or enter the address manually.

### Manual

1. Copy the `custom_components/surplife_ble/` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration through **Settings → Devices and Services → Add Integration**.

## Configuration

Configure the integration through the UI:

1. Go to **Settings → Devices and Services → Add Integration**.
2. Search for "Surplife BLE".
3. Select a discovered device from the list. Or enter the BLE address manually.

> **Tip:** If discovery does not find your device, use `ble-rgb scan` or a BLE scanner app to find its address.
> The device advertises as `IOTBT` followed by a hex string.
> For example, `IOTBT144`.

## Technical details

| Property | Value |
| --- | --- |
| Protocol | IOTBT EA81 (27-byte state frame, magichome2 framing) |
| GATT service | `0xFFFF` |
| Write characteristic | `FF01` |
| Notify characteristic | `FF02` |
| Connection | `bleak-retry-connector` for reliable BLE connections |
| Polling interval | 15 seconds |
| IoT class | `local_polling` |

## Known limitations

- RGB color state is assumed.
- RGB color changes do not update the device `EA81` state frame.
- The HA entity uses `assumed_state`.
- The user interface shows the last command, not the device state.
- This is a firmware limitation.
- The Android app writes a JSON state frame to `FF02`.
- `FF02` is a notify-only characteristic.
- Android permits this write.
- macOS, Linux, and ESPHome reject this write.
- Use one controller at a time.
- The device must be the only controller of the lamp.
- Then the state stays consistent.
- HA does not detect color changes from the physical button or another app.
- Power and white brightness update on the next poll.
- Brightness in color mode sends the full color command again.
- It does not send a separate brightness update.

## Troubleshooting

### Device not discovered

- Ensure the light is powered on and within BLE range.
- Check that your Bluetooth adapter works in HA. Go to **Settings → System → Bluetooth**.
- Try entering the address manually. Use `ble-rgb scan` or a BLE scanner app.
- The device advertises as `IOTBT` followed by a hex string. For example, `IOTBT144`.

### Commands not working

- The power on and power off commands work. If they do not work, check that the device is connected.
- Color changes work. The state can show as "assumed". This is expected.
- If the light does not respond, switch it off and on. Then reload the integration.

### State shows wrong color after restart

- This is a known limitation. The device state frame does not show `3B` color commands.
- Send a color command from HA to correct the display.

## Development

This repository also ships a standalone `ble-rgb` command-line interface.
Use it to investigate the protocol and to debug.
Install it for development with:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,reference]'
```

On macOS, grant Bluetooth access to the terminal application that starts Python.
macOS uses a CoreBluetooth UUID in place of a public Bluetooth MAC address.

### Safe investigation workflow

Use these commands to investigate the protocol safely:

```bash
ble-rgb scan --duration 15 --json captures/scan.json
ble-rgb inspect --device COREBLUETOOTH-UUID --json captures/gatt.json
ble-rgb probe --device COREBLUETOOTH-UUID --allow-probe-write \
  --jsonl captures/probe.jsonl
```

`inspect` does not write.
`probe` sends known state-query candidates, one at a time, after it subscribes to notification characteristics.
Review the output before you use `on`, `off`, `color`, `white`, or `brightness`.

For an unknown protocol, the safest method to decode it is state-diffing.
Set the lamp to known states through the vendor app.
Probe from the Mac at each state.
Then compare the states with `ble-rgb diff`.
This shows which bytes encode power, color, and brightness.
For automated capture, use `ble-rgb watch`.
It monitors state changes continuously while you cycle through colors in the app.
Then use `ble-rgb analyze` to produce a full field map.
See `docs/CAPTURE_WORKFLOW.md`, section 2b.

For an unknown protocol, capture Android HCI snoop traffic.
Perform one isolated action at a time in the Surplife app.
Convert the snoop log with `ble-rgb hci-convert`.
See `docs/CAPTURE_WORKFLOW.md`, section 4.
Review every TX record.
Use `replay --dry-run` before you send data to the device.

## Contributing

Pull requests are welcome.
Run `ruff check` and `pytest` first.

```bash
ruff check .
pytest
```

## License

[MIT](LICENSE). For protocol research sources, see [NOTICE.md](NOTICE.md).
