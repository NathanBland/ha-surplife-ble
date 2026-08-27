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
- Byte 6 is power: `0x23` means off, `0x24` means on.
- Byte 11 is hue on a `0` to `180` scale.
- Byte 12 is saturation.
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

- The command family for this DX23012 is `0x0A`.
- The header is `E1 03 00 <segment_count> 00 00 <segment_count>`.
- Each segment record is `A1 <hue> <saturation> <brightness>`.
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

## 7. Next steps

1. Add an `E1 03` encoder to `custom_components/surplife_ble/core/protocol.py`.
2. Add a client method that builds and sends the segment command.
3. Use the `0x0A` transport family and 20 segments for the DX23012 codec.
4. Route RGB color and RGB brightness commands through `E1 03`.
5. Query the `EA 81` state after each command.
6. Mark the state as confirmed when the response matches the command.
7. Keep the assumed-state overlay only when confirmation fails.
8. Test the same command on the second lamp.
9. Test through the ESPHome Bluetooth proxy and a direct USB adapter.
10. Add unit tests for the encoder, frame length, and segment records.
11. Keep the `3B A1` command as an experimental fallback until the second lamp passes the same tests.

## 8. Safety

- Use only the verified device for new tests.
- Send reviewed application-level ATT writes one at a time.
- Do not replay complete connection transcripts.
- Do not send firmware or mesh commands.
- Confirm the MTU and characteristic properties before long writes.
