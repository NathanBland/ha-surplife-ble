# Surplife BLE

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This integration controls Surplife and MagicHome2 Bluetooth LED lights in Home Assistant.
It supports the MIHEAL DX23012 floor lamp and similar devices that use the IOTBT BLE protocol.

## Features

| Capability | Status |
| --- | --- |
| Power on and off | Verified on DX23012 |
| RGB color | Verified on one DX23012 |
| White brightness | Needs hardware retest |
| Overall brightness | Verified on one DX23012 |
| State confirmation | Verified on one DX23012 |
| Multiple devices | Supported |
| Automatic discovery | Supported |
| Manual setup | Supported |

## Supported devices

- MIHEAL DX23012 floor lamp with the verified IOTBT state signature

Other `IOTBT` devices can use different commands. An `IOTBT` name alone does not prove compatibility.

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

> **Tip:** If discovery does not find your device, use a BLE scanner app to find its address.
> The device advertises as `IOTBT` followed by a short string. For example, `IOTBT144`.

## Known limitations

**Color state may show as "assumed" on the first command.**

The integration sends a color command and completes a short state query.
The query prepares the lamp for the next color or brightness command.
If confirmation fails, the integration shows an assumed state and requests a background refresh.
A matching poll confirms the state. The assumed state expires after five seconds.

**Use one controller at a time.**

The lamp accepts one active BLE controller at a time.
The integration keeps its connection open for faster commands.
Each connected lamp uses one Bluetooth adapter or proxy connection slot.
Close the vendor app before you control the lamp from Home Assistant.
External changes appear after the assumed state expires and a poll succeeds.

**The first connection can take several seconds.**

The integration keeps a successful connection open.
Tests measured later color and brightness commands at approximately 0.2 to 0.8 seconds.

**Brightness in color mode sends the full color command again.**

The device does not accept a standalone brightness command in color mode.
The integration resends the current color with the new brightness value.

## Troubleshooting

### Device not discovered

- Make sure the light is powered on and within Bluetooth range.
- Make sure your Bluetooth adapter works in Home Assistant. Go to **Settings → System → Bluetooth**.
- Enter the address manually. Use a BLE scanner app to find it.
- The device advertises as `IOTBT` followed by a short string. For example, `IOTBT144`.

### Commands do not work

- Power on and power off should always work. If they do not, make sure the device is connected.
- Color changes work on a verified DX23012 state signature.
- An assumed state means that the lamp did not send a matching notification.
- A background refresh confirms or corrects the state.
- If the light does not respond, power cycle it. Then reload the integration.

### State shows the wrong color after a restart

The integration queries the device state on startup.
If the device is not reachable, the state stays empty until the next successful poll.
Send a color command from Home Assistant to update the display immediately.

## Development

This repository includes a standalone `ble-rgb` command-line tool.
Use it to investigate the protocol and to debug.
For protocol details, GATT characteristics, and command formats, see [docs/CAPTURE_WORKFLOW.md](docs/CAPTURE_WORKFLOW.md).
For the verified DX23012 command findings, see [docs/DX23012_PROTOCOL_FINDINGS.md](docs/DX23012_PROTOCOL_FINDINGS.md).

Install the development environment:

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

`inspect` does not write to the device.
`probe` sends known state queries, one at a time, after it subscribes to notifications.
Review the output before you use `on`, `off`, `color`, `white`, or `brightness`.

For an unknown protocol, the safest method is state-diffing.
Set the lamp to known states with the vendor app.
Probe from the computer at each state.
Then compare the states with `ble-rgb diff`.
For automated capture, use `ble-rgb watch` and `ble-rgb analyze`.
See [docs/CAPTURE_WORKFLOW.md](docs/CAPTURE_WORKFLOW.md) for the full workflow.

## Contributing

Pull requests are welcome.
Run `ruff check` and `pytest` before you submit a pull request.

```bash
ruff check .
pytest
```

## License

[MIT](LICENSE). For protocol research sources, see [NOTICE.md](NOTICE.md).
