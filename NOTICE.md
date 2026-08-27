# Protocol research acknowledgements

This project independently implements packet formats documented by these upstream projects:

- `lightinglibs/flux_led` (LGPL-3.0): classic LEDENET checksums, state layouts, and the
  extended `EA 81` state format.
- `Bluetooth-Devices/led-ble`: Home Assistant-oriented BLE LED discovery and protocol
  resolution behavior.
- `rabidpaperclip/magichome2-ble` (MIT): MagicHome2 `FFFF`/`FF01`/`FF02` GATT profile and
  sequence/length BLE envelope.
- `8none1/lednetwf_ble` (MIT): the eight-byte MagicHome2 v0 transport header, JSON-wrapped
  notifications, and the `EA 81 8A 8B` IOTBT state-query candidate.

The btsnoop/HCI parser in `core/hci.py` is implemented from the btsnoop file format
specification and the Bluetooth Core Specification (ATT/L2CAP layers). No external
snoop-parsing library is used.

The open `flux_led` PR #544 supplies non-authoritative `E1 22` golden packet references.
They remain marked experimental for the DX23012 until verified against physical hardware.

The authoritative DX23012 fixture was captured on 2026-08-26 from device `IOTBT144`.
It uses service `FFFF`, write characteristic `FF01`, notify characteristic `FF02`, and
returns a JSON-wrapped 27-byte `EA 81` state. Its unknown layout is retained without
assigning IOTBT mesh-address/mode bytes to model, firmware, or RGB fields.
