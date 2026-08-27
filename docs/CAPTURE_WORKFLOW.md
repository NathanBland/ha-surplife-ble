# DX23012 capture and acceptance workflow

## 1. Establish the exact device

1. Power off other nearby RGB lights.
2. Run `ble-rgb scan --duration 15 --json captures/scan.json` twice: once with the
   lamp powered off, then again with it powered on.
3. Compare the two files and record the CoreBluetooth UUID, local name, service UUIDs,
   manufacturer data, and service data that appear only with the lamp powered on.
4. Run `ble-rgb inspect --device DEVICE_ID --json captures/gatt.json` and confirm the
   exact writable and notify characteristics. Do not infer handles from another model.

## 2. Query before control

Run:

```bash
ble-rgb probe --device DEVICE_ID --allow-probe-write \
  --jsonl captures/probe.jsonl
```

A control command remains locked unless two responses agree on frame family and model.
Review the transcript for `resolution.repeatable: true`. Record whether the winning
query was raw, MagicHome2 standard-length, or MagicHome2 length-plus-two.

For the physical DX23012 captured on 2026-08-26, `classic-magichome2` produced a
repeatable JSON-wrapped `EA 81` response on `FFFF/FF01` → `FF02`. The state fixture is
stored as `dx23012_iotbt144` in `tests/fixtures/protocol_packets.json` and is authoritative
for this lamp. Do not interpret byte `0xAA` as a model or byte `0x09` as firmware: in the
IOTBT DeviceState2 layout, bytes 3-4 are a mesh address and byte 5 is a mode. Color bytes
remain unresolved, and control stays locked until Telink-vs-segment command selection is
verified.

## 2b. Decode the state layout by diffing

Before attempting any control command, decode the 27-byte `EA 81` state layout by
probing the lamp at different physical states and comparing the results. Use the
Surplife app on a phone only to set the lamp state — the Mac captures the state.

### Automated watching (recommended)

Run `ble-rgb watch` once, then cycle through colors and modes in the Surplife app
at your own pace. The tool automatically detects and logs every state change:

```bash
ble-rgb watch --device DEVICE_ID --interval 2 --timeout 300 \
  --jsonl captures/watch-session.jsonl
```

Change states in the app (off, on, red, blue, green, white, dim, bright, effects,
etc.). The tool prints each transition in real time and logs it to the JSONL file.
Press Ctrl+C to stop early.

Then batch-analyze all captured states at once:

```bash
ble-rgb analyze --captures captures/watch-session.jsonl
```

This produces a full field map showing which byte positions are static vs dynamic,
all distinct values seen at each position, and the current confidence level for
each field annotation. It also prints **recommended next captures** — specific
app actions to try, ordered by priority:

- **HIGH**: needed to unlock basic control (e.g. "set a pastel color to confirm
  saturation", "set green and yellow to confirm the hue scale")
- **MEDIUM**: expands control coverage (white color temperature, scenes, rhythm)
- **LOW**: exploratory (scribble segments, mesh linking)
- **SKIP**: app features that probably don't map to state fields (schedule,
  playlist, settings)

Follow the HIGH recommendations first, re-run `watch` or `probe` for each one,
then re-run `analyze` to see if the recommendations change. When all HIGH
items are cleared, the tool reports "Control may be ready to unlock."

### Manual diffing (for targeted verification)

For comparing two specific states, use `ble-rgb diff`:

```bash
ble-rgb probe --device DEVICE_ID --allow-probe-write \
  --jsonl captures/state-off.jsonl
# ... change state in app ...
ble-rgb probe --device DEVICE_ID --allow-probe-write \
  --jsonl captures/state-red-dim.jsonl
ble-rgb diff --a-capture captures/state-off.jsonl \
  --b-capture captures/state-red-dim.jsonl \
  --label-a off --label-b red-dim
```

### What to capture

Map the layout by isolating one variable at a time:

- **off vs on** → confirms the power byte (position 6, `0x23`/`0x24`).
- **red-dim vs red-bright** → reveals brightness bytes.
- **red vs blue vs green** → reveals color bytes (HSV hue at pos 11, 0-180 scale).
- **red vs white** → reveals the mode byte that distinguishes color from white.
- **pastel colors** → confirm saturation (pos 12).
- **warm vs cool white** → check if pos 8 encodes color temperature.

After each diff, update the field annotations in
`custom_components/surplife_ble/core/statediff.py` to reflect what was
confirmed. Promote fields from `[hypothesis]` or `[unknown]` to
`[confirmed]` only after the same byte changes predictably across at least
two independent capture pairs.

Do not send any control command until at least power, color, and brightness
are confirmed by diffing. The state-diff path is read-only and cannot damage
the device.

## 3. Core control acceptance

Perform one action at a time and leave at least two seconds between actions:

1. `off`, then `on`.
2. `color --rgb 64,0,0 --brightness 20`.
3. `white --brightness 20`.
4. `color --rgb 255,0,0 --brightness 100`.
5. Brightness 20, 50, and 100.
6. Power-cycle the lamp and repeat the state query.

Stop immediately if a command changes an unexpected segment, enters an effect, stops
notifications, or causes the device to disappear. Preserve the JSONL file unchanged.
Do not try `--experimental-e122` until the normal path is understood.

## 4. Android HCI fallback

If none of the state queries works:

1. Enable Bluetooth HCI snoop logging in Android Developer Options.
2. Toggle Bluetooth off and on to start a clean session.
3. Open Surplife and wait for connection.
4. Perform exactly one action, wait five seconds, then perform the next action.
5. Capture, in order: query/refresh, off, on, dim red, dim white, full red, and three
   brightness values.
6. Export an Android bug report and extract `btsnoop_hci.log`.
7. List the BLE connections found in the snoop log:

   ```bash
   ble-rgb hci-convert --snoop btsnoop_hci.log --device DEVICE_ID --list-connections
   ```

   Identify the lamp's connection handle by matching its attribute handles to the
   GATT profile from `inspect --json` (FF01=handle 22, FF02=handle 19 for the DX23012).
8. Convert the snoop log to JSONL with handle-to-UUID mapping:

   ```bash
   ble-rgb hci-convert --snoop btsnoop_hci.log --device DEVICE_ID \
     --gatt captures/gatt.json --connection-handle 0x0040 \
     --jsonl captures/hci-imported.jsonl
   ```

   Review every TX record before replay. Annotate each with the app action it
   corresponds to (off, on, dim red, etc.) by editing the `note` field in the JSONL.
9. Dry-run the replay to see exactly what would be sent:

   ```bash
   ble-rgb replay --device DEVICE_ID --capture captures/hci-imported.jsonl \
     --allow-raw-write --dry-run --limit 1
   ```

10. Replay one reviewed TX record at a time against the real device:

    ```bash
    ble-rgb replay --device DEVICE_ID --capture captures/hci-imported.jsonl \
      --allow-raw-write --limit 1 --jsonl captures/replay-traffic.jsonl
    ```

    Increase `--limit` only after each record produces the expected physical result.
    Stop immediately if a command changes an unexpected segment, enters an effect,
    stops notifications, or causes the device to disappear.

Never replay the complete connection transcript. Replay only reviewed application-level
ATT writes, one at a time, using the exact device identifier and characteristic.

## 5. Home Assistant matcher gate

Automatic discovery can be enabled only when the two scans establish a sufficiently
narrow matcher. Prefer stable manufacturer/service data. A local name or `FFFF` service
alone is not specific enough. Add that matcher to `manifest.json`, add discovery fixtures,
and verify that unrelated LEDnet/Surplife devices are rejected.
