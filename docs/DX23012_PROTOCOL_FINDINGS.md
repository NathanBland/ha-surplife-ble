# DX23012 protocol findings

## 1. Verified device

- CoreBluetooth UUID: `925FACF3-6423-93DA-A07D-52803B9BABF2`.
- Local name: `IOTBT144`.
- Related names seen: `IOTBT04F`.
- Device: MIHEAL / Dongguan DaXian DX23012 floor lamp.
- Primary service `0000ffff-0000-1000-8000-00805f9b34fb`.
- Write characteristic `FF01`: `0000ff01-0000-1000-8000-00805f9b34fb`.
- Notification characteristic `FF02`: `0000ff02-0000-1000-8000-00805f9b34fb`.
- Negotiated MTU: 200.
- Maximum write-without-response length: 197 bytes.

## 2. HCI parser corrections

The first pass of the Android HCI snoop parser used wrong ATT opcodes and reversed direction.
The following values are now confirmed:

- Android app writes commands to `FF01`.
- Lamp sends state notifications from `FF02`.
- Write Command ATT opcode is `0x52`.
- Notification ATT opcode is `0x1B`.
- Indication ATT opcode is `0x1D`.
- Direction bit `0` means host-sent / TX.
- Direction bit `1` means host-received / RX.
- btsnoop timestamps are absolute. Subtract the btsnoop epoch before conversion.

These corrections showed the captured traffic from the Android app was a sequence of real write commands to the writable characteristic.

## 3. State format

- State frames use the `EA 81` prefix.
- The full state frame is 27 bytes.
- Bytes 3 and 4 contain a mesh address. Do not use them as a fixed device signature.
- Byte 5 identifies the verified IOTBT state layout as `0x09`.
- Byte 6 is power: `0x23` means on, `0x24` means off.
- Bytes 11 and 12 contain packed hue and saturation.
- Hue is the packed value shifted right by seven bits.
- Saturation is the low seven bits of the packed value.
- Byte 13 is brightness.
- Color and white modes are distinguished by the white-state field, not by a zero RGB value.

## 4. Control commands

### 4.1 Old `3B A1` RGB command

- The `3B A1` command changes the physical output.
- It does not update the lamp's stored `EA 81` state.
- The next state query returns the old RGB values.
- This caused stale UI values before the assumed-state overlay was added.

### 4.2 New `E1 03` segment color command

The Android app sends a segmented color command.

- The Android capture from `IOTBT144` used command family `0x0A`.
- Hardware tests show that command family `0x0B` works on both test lamps.
- `IOTBT04F` disconnects and ignores the segment command with family `0x0A`.
- The header is `E1 03 00 <segment_count> 00 00 <segment_count>`.
- Each segment record is `A1 <packed_hs_high> <packed_hs_low> <brightness>`.
- The tested lamp uses 20 segments.
- A blue at 40% brightness used 20 copies of `A1 78 64 28`.
- This produced a 95-byte framed write. That size is below the MTU limit.

## 5. Hardware test result

The test was performed on 2026-08-26.

Steps:

1. Start `ble-rgb shell` for the known device.
2. Let the shell resolve the codec to `iotbt-ea81`.
3. Send the `E1 03` payload through the shell.
4. Query the `EA 81` state.

Result:

```text
power: true
rgb: [0, 0, 102]
brightness: 102
white: 0
assumed: false
raw_hex: ea810100aa0923240150f0786428006405001dffffffff01000000
```

The test confirms the `E1 03` command changes both the physical output and the stored state.
The lamp reports the new color without local guessing.

## 6. Why the one-shot test failed

A single raw write from a fresh connection did not update the state.
The persistent shell succeeded because it completes the full session: connect, discover services, resolve the codec, subscribe to notifications, and then issue the command.
The one-shot path likely skipped the notification subscription or codec exchange.
Each step was not tested in isolation, so this is the current hypothesis.

## 7. Implementation status

The integration now uses `E1 03` for RGB color and RGB brightness on the verified DX23012 state signature.
It uses transport family `0x0B` and 20 segment records for RGB commands.
State queries continue to use the resolved probe transport.
It subscribes to notifications before each command session.
It waits 0.15 seconds for a matching notification after an RGB command.
It then sends up to two short state queries to complete the required command round trip.
A typical connected state query completes in less than 50 ms.
It requests a background refresh only when the bounded confirmation fails.
The assumed RGB state expires after five seconds.
The next matching poll confirms the state before expiration.

The integration continues to use the legacy `3B B1` candidate for dedicated white mode.
The dedicated white command still requires a hardware retest.
An `E1 03` command with zero saturation is not verified as a dedicated white command.

## 8. Performance test results

Tests used one persistent direct Bluetooth connection for each lamp.
Each sequence used red, blue, green, purple, and two brightness changes.

`IOTBT04F` completed connected commands in 180 to 778 ms.
Its later state polls completed in 41 to 156 ms.
`IOTBT144` completed connected commands in 210 to 480 ms.
Its later state polls completed in 39 to 69 ms.
All final states matched purple at 90% brightness.
Both connections stayed active during the corrected sequences.

A burst without state queries failed on `IOTBT04F`.
Only the first long segment command was committed.
Thus, each segment command requires a state-query round trip before the next segment command.
Initial connection and protocol resolution can take several seconds.

## 9. Remaining validation

1. Test dedicated white mode after an RGB command.
2. Test RGB mode after a white command.
3. Test through an ESPHome Bluetooth proxy.
4. Confirm notification registration after an unexpected disconnect.
5. Measure rapid slider updates with the Home Assistant interface.
6. Keep `3B A1` as an experimental fallback until white-mode tests are complete.

## 10. Safety

- Use only the verified device for new tests.
- Send reviewed application-level ATT writes one at a time.
- Do not replay complete connection transcripts.
- Do not send firmware or mesh commands.
- Confirm the MTU and characteristic properties before long writes.
