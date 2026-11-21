# Surplife 1.6.5 APK: BLE protocol findings

Source: `Surplife_1.6.5_APKPure.apk`, analysed statically with REA on 2026-11-20.
The app is a Zengge MagicHome2 Flutter build. Command logic runs in Dart AOT (`libapp.so`), which has class and package names but no function symbols. The BLE transport is Java (`com.zengge.hagallbjarkan`), and this document traces it in full.

All APK strings and code were treated as untrusted data.

Evidence key:
- **[J]** Java class/method, decompiled through REA.
- **[A]** a key in a bundled asset JSON (`assets/flutter_assets/packages/...`).
- **[D]** a Dart string in `libapp.so` (a name only; behaviour inferred).
- **[R]** a repo `file:line`.

Confidence: **Confirmed** = byte-exact from code or template. **Inferred** = consistent evidence, but Dart decides the final value. **Unknown** = not recoverable statically.

---

## 1. Device identity in the app

| Item | Value | Evidence |
|---|---|---|
| Product ID | 170 (state byte 4 = `0xAA`) | [A] `magichome2_home_data_provide/assets/ble_devices.json` |
| Category | `zm` / `hc3` (symphony / RGBIC addressable) | same |
| Protocols | `common`, `common1_0`, `common2_0`, `symphonyE1` | same |
| State protocol | `wifibleSymphony` ("统一命令2.0 灯具标准状态", the unified 2.0 standard light state) | same |
| Functions (minVer) | `switch_led_v2/v3`, `bright_value_v2`, `colour_data_v2/v3` (Hex), `static_colorful_hc3`, `smear_hc3`, `smear_hc3_v2` (≥6), `custom_data_hc3`, `music_data_hc3`, `linkage_hc3`, `multiple_screen_data` (≥2), `set_wiring_preview`, `set_wiring_v2`, `function_setting` (≥9), `function_inquiry`, timers, `microphone_V2` | same |
| GATT | service `0000ffff-…`, write `0000ff01-…`, notify `0000ff02-…` | [J] `ZGHBWriteUtils` (`SERVICE_UUID`/`WRITE_UUID`), [D] UUID strings |

PID 170 has no `scene_data_v2/v3` entry in `ble_devices.json`. Its effects run through the `hc3` symphony functions instead (§6).

---

## 2. Transport: how a command leaves the app

### 2.1 Call chain (Confirmed, [J])

1. Dart calls the Pigeon API `FlutterDeviceControlApi.pushCommand(mac, cmdId, hexPayload, reliable, …)`.
2. `com.zengge.magichome2.server.devicemanager.ble.m.apply` calls `deviceHandler.write(mac, cmdId, hexToBytes(payload), reliable)`.
   - **Dart chooses cmdId and `reliable` per command.** They are not visible statically.
   - If a crypto context is present, the payload first goes through `BleDeviceManagerServer.xteaCtrTransform` (XTEA-CTR). This applies only to devices that negotiate a secure session; nothing suggests PID 170 does, and the repo's plaintext writes work.
3. `ZGHBDeviceHandler.write` calls `ZGHBWriteUtils.write(seq = sequenceUtils.nextByteSeq(mac), cmdId, payload, conn, reliable, writeVersion, protect)`.
   - The sequence is one byte per device, wrapping.
4. `ZGHBWriteUtils.write`:
   - `UpperTransportLayer.createUpper(ack=false, protect, seq, cmdId, payload)`, type 0.
   - Fragments it with `LowerTransportLayerEncoder.generator` (V0) or `generatorV1`.
   - Max fragment length: `getTransportMaxLength` = `min(MTU, 255)` for V0, `min(MTU, 512)` for V1.
   - Writes each fragment with `conn.write(FFFF, FF01, frag, reliable).join()`. It **stops on the first failed fragment, with no retry and no ack wait.**
5. `ZGHBDeviceHandler.getWriteVersion(bleVersion)` returns 1 (V1) if the device BLE version is ≥ 8, otherwise 0.
6. `HBConnectionImpl.writeValue` sets the GATT write type: `reliable ? WRITE_TYPE_DEFAULT (with response) : WRITE_TYPE_NO_RESPONSE`.
   - Writes are serialised in `writeMessagesQueue`.
   - The next write starts only on `onCharacteristicWrite`.

### 2.2 Frame layout (V0, Confirmed, [J] `LowerTransportLayerEncoder`)

Single (unsegmented) frame:

```
[0] ctrl   [1] seq   [2..3] 0x80 0x00   [4..5] payload_len (BE)   [6] payload_len+1   [7] cmdId   [8..] payload
```

Bits of the `ctrl` byte:

| bit | meaning |
|---|---|
| 6 | segmented |
| 5 | protect (encrypted payload) |
| 4 | ack requested |
| 3..2 | type: 0 = normal, 1 = HEX, 2 = ACK_PACK |
| 1..0 | framing version (1 = V1) |

Segmented frames:
- **First frame:** ctrl has bit6 set, `[2..3] = 0x0000`, `[4..5]` = **total** length, `[6]` = this chunk's length + 1, `[7]` = cmdId.
- **Later frames:** `[0] ctrl, [1] seq, [2..3] segment index` (with `0x8000` OR-ed in on the last one), `[4] chunk length`, then the data.

There is **no checksum at the transport layer.** Checksums exist only inside the inner command, where `needChecksum` in the templates appends an additive sum (§3).

### 2.3 Acks and responses (Confirmed, [J])

- **A normal write never waits for an ack.** The app sends ctrl bit4 = 0.
- `ZGHBWriteUtils.writeAck` is a separate path that sends with bit4 = 1 and always uses V0 framing and write-without-response.
  - It waits up to 500 ms for a decoded packet of type 2 (ACK_PACK).
  - On timeout it sets `frag[0] |= 0x80` as a retransmit marker and resends **until it succeeds**.
- **Request/response:** `ZGHBDeviceHandler.request(mac, respCmdId, cmdId, payload, reliable)` subscribes to `respCmdId`, writes, and waits `DEFAULT_TIMEOUT_FOR_ASYNCHRONOUS_MESSAGES_IN_TRANSMIT` for a notification with that cmdId.
  - Dart also keeps `_expectedResponseCmdIds` and `sendCommandWithResponse`. [D]
- **Receive path:** `ZGHBReceiveCallback.onCharacteristicChanged` → `LowerTransportLayerDecoder.getTransport` (or `getTransportV1` when `ctrl & 3 == 1`).
  - Unsegmented frames must have length ≥ 7 and `[2..3] == 0x8000`.
  - Payload length is read from `[4..5]`.
  - Segments are reassembled by seq and contiguous index.
  - **Type-2 (ack) packets are dropped.** Everything else goes to `postSubscriber(cmdId, payload)` and on to Dart.

**How the app confirms state:** there is no per-command ack. After a write, the device pushes or answers a state frame (`ea 81 …`, `wifibleSymphony`), and Dart updates the UI from it. The `state_upload_v2` query is `ea 81 8a 8b` with `responseCount: 1` ([A] `wifi_dp_cmd.json`).

---

## 3. Inner command templates

From [A] `wifi_dp_cmd.json` / `ble_dp_cmd.json`. `+cs` means an additive 8-bit checksum is appended.

| Function | Template | cs | Fields |
|---|---|---|---|
| `switch_led_v3` | `e0 01 {preview} 3b {open} 00 00 00 00 00 {gradient:3} {delay:2}` | no | preview 0–255, delay 0–65535, gradient 0–16777215 |
| `switch_led_v2` | `3b {open} 00 00 00 00 00 {gradient:3} {delay:2}` | yes | |
| `bright_value_v2` | `3b 01 00 00 {value} 00 {value} {delay:3} {gradient:2}` | yes | value 0–100. BLE `opcode: 11` (0x0B) in `ble_dp_cmd.json` |
| `temp_value_v2` | `3b b1 00 00 00 {cct} {bright} 00 00 1e 00 00` | yes | cct 0–100, bright 0–100 |
| `scene_data_v3` | `e0 02 {preview} {model} {speed} {bright}` | no | model 37–56, speed 0–100, bright 0–100 |
| `scene_data_v2` | `38 {model} {speed} {bright}` | yes | speed 1–31, bright 1–100 |
| `state_upload_v2` | `ea 81 8a 8b` | no | responseCount 1 |
| `get_symp_config_v2` | `63 12 21 f0` | yes | responseCount 14 |
| `function_inquiry` | `2b 2c 2d` | — | |
| `colour_data_v2/v3`, `static_colorful_hc3`, `smear_hc3(_v2)`, `custom_data_hc3`, `set_wiring_preview` | **`type: "Hex"`, `value: {}`, built in Dart** | — | Bytes not in assets. Builders named in [D]: `buildColorHEXCommand`, `buildSmearHEXV2Command`, `cmdSmearSingleEffectType`, `CommandWiringTypeParams`, `ModeE1Strategy`, `buildLedCountCalibrationHEXCommand` |
| `set_wiring_v2` | Json fields `wiring` (4–6, step 2) and `sort` (1–3) | — | "如果wiring字段的min或max为null代表不支持wiring" (null min/max means wiring is not supported). The byte form is not in assets. |

**About `{open}`:** the templates don't define its encoding. MagicHome uses `0x23` = on and `0x24` = off throughout (the repo's `POWER_ON`/`POWER_OFF`), so these are the likely values. *Inferred.*

**About the `cmdId` (transport "family") byte:** `bright_value_v2` carries `opcode: 11` (0x0B) in `ble_dp_cmd.json`, and `net_state`/`reset` carry opcodes 7/8. So BLE cmdIds are set per command, and **0x0B is the cmdId for unified-protocol (3B/E0/E1) light commands.** This matches the repo's hardware finding that `0x0B` works on both lamps while `0x0A` disconnects IOTBT04F ([R] `docs/DX23012_PROTOCOL_FINDINGS.md:57-59`). *Inferred, strong.*

---

## 4. Responses

From [A] `wifi_dp_response.json`:

- **`get_symp_config_v2`** gives `{supportType} 63 {pointCount:2} {part:2} {icType} {sort} {musicPart:2} {cs}`:
  - `pointCount` = pixel count (u16 BE)
  - `part` = segment count (u16)
  - `icType` = index into the JS `symphonyIC` table (1 universal, 2 SM16703P, 3 WS2812E, 4 UC1903B, 5 RGBW universal, 9 RGBCW universal, 21 WS2805, …)
  - `sort` = colour order
  - `musicPart` = segments used in music mode
  - **This is how the app learns the strip's real layout.**
- **Legacy `state_upload`** (`81`, 14 bytes): `81 {devType} {open} {mode} {wiring_sort} {speed} {r} {g} {b} {warm} {ver} {cool} {flag} {cs}`.
- **`wifibleSymphony` (`ea 81`)**: the repo already parses this (§5). The app's parser for it is in Dart and not recovered.

---

## 5. Differences from the repo

| # | Area | App (APK 1.6.5) | Repo | Impact |
|---|---|---|---|---|
| D1 | **Power command** | `switch_led_v3` = `e0 01 {preview} 3b {open} 00×5 {gradient:3} {delay:2}` | `iotbt_power` = `e0 01 00 {23\|24} 00×10`. **The `3b` byte is missing**, so the open byte sits one position early ([R] `core/protocol.py:404-410`) | High. The repo sends the old "extended zone power" form. It may work on some firmware, but it is not what the app sends to PID 170. |
| D2 | **cmdId for power/white** | Unified commands use cmdId 0x0B (§3) | `_send_inner` defaults to `command_family=0x0A` ([R] `core/client.py:294`). Only `set_rgb` passes `0x0B` ([R] `core/client.py:573`). `set_power` and `set_white` use 0x0A ([R] `core/client.py:526,654-655`) | High. This is the most likely reason white is "unverified" and power is unreliable on IOTBT04F, which disconnects on 0x0A. |
| D3 | Write type | Per command, chosen by Dart (`reliable`): with-response when reliable, otherwise without | Always write-without-response when available ([R] `core/client.py:272-275`) | Low. Both are valid. The app also serialises writes on write-complete, while bleak does not without `response=True`. |
| D4 | Write pacing | One GATT write in flight, then wait for `onCharacteristicWrite` ([J] `HBConnectionImpl.writeNext`) | Fire and forget, plus a state round trip after E1 ([R] `core/client.py:607-636`) | Medium. The app's pacing explains the earlier finding that "a burst without queries failed". The repo's query workaround stands in for that pacing. |
| D5 | Segmented TX | Fragments to `min(MTU,255)` (V0) or `min(MTU,512)` (V1) using segment headers | No fragmentation. One frame must fit in a single write ([R] `core/framing.py:39-62`) | Medium. With 20 segments the frame is 95 B, which is fine. Large segment counts (`part` > ~46 at MTU 200) would need segmentation. |
| D6 | RX decoding | Checks ctrl bits, **drops ack packets (type 2)**, reassembles segmented notifications by index, and uses `[4..5]` as the length | Ignores ctrl, takes length from `[6]`, and doesn't handle continuation frames whose `[2..3]` ≠ `0x8000` ([R] `core/framing.py:65-77,163-174`) | Medium. Long responses (timer lists, IC config on some firmware) would be dropped, and an ack-type packet could be misread. |
| D7 | Retry | None for normal writes. Only `writeAck` retries, with bit 7 set | None | None. The behaviour matches. |
| D8 | V1 framing | Used when the device's bleVersion ≥ 8 | V0 only | Low. V0 works on both test lamps, so these lamps are V0 (or accept V0). |
| D9 | Strip layout | Reads `63 12 21 f0` to get pixels, segments, IC and order | Hard-coded 20 segments ([R] `core/protocol.py:50`). `led_count` is taken from `ea81[18]` ([R] `core/protocol.py:230`) | Medium. On strips with a different `part` count, E1 colour fills only some segments. |
| D10 | White / CCT | `temp_value_v2` = `3bb1…` + cs | Template is identical ([R] `core/protocol.py:351-376`), but it is sent with cmdId 0x0A (see D2) | Template is fine. The cmdId is the likely fault. |
| D11 | Brightness | `bright_value_v2` exists with cmdId 0x0B | Repo comment: "3B 01 … not accepted by this device" ([R] `core/client.py:677-679`). It was most likely tested with 0x0A | Worth retesting with 0x0B. |
| D12 | Effects | `hc3` symphony functions (§6); `scene_data_v3` `e0 02` for unified scenes | None | Missing feature. |

---

## 6. Segments, colour profile and effects

### 6.1 Segment control

- The device reports its segment count as `part` in `get_symp_config_v2` (§4).
- The repo's `E1 03` frame (header `E1 03 00 N 00 00 N` + N × `A1 hs_hi hs_lo bright`, [R] `core/protocol.py:507-562`) matches the app's `symphonyE1` protocol family.
  - It was verified on hardware against an app capture.
  - The app builds it in Dart (`ModeE1Strategy` [D]), so no static template exists.
- **Per-segment colour** means sending different `A1` records in the same frame. The repo's record layout already supports this.
- What `N` means in header bytes 3 and 6 is *inferred*: (start/count, count). Statically, it is consistent with "number of records".

**The MIHEAL corner lamp (DX23012, model ZJ-BWCG-RGB-R24B1)**

- The Amazon listing says only "RGB+IC", "4 light units", 12 V 1 A and a "DIY mode". It gives no pixel or segment count.
- The app capture that the repo copied sent **20 segment records**. The app sizes this frame from the device's `part` value, so this lamp most likely reports `part = 20`.
- Its state frame has `0x1d` (29) at byte 18 (`ea810100aa…`, [R] `docs/DX23012_PROTOCOL_FINDINGS.md:85`). The repo reads that byte as `led_count`, but it doesn't match 20. Byte 18's meaning is *Unknown*.
- `get_symp_config_v2` will report the true pixel and segment counts (phase 5).
- **The app's "DIY mode"** is the per-segment painting UI (`smear_hc3` / `custom_data_hc3`).
  - A static per-segment pattern needs no new protocol. It is one `E1 03` frame whose records differ, e.g. segments 0–4 red and 5–9 blue.
  - Animated DIY effects use the unrecovered `custom_data_hc3` bytes.
- **Planned Home Assistant surface:**
  - (a) A `surplife_ble.set_segments` service that takes a list of colours (or index → colour). Segments not given keep their last known colour, which the integration caches because the state frame reports only one colour.
  - (b) Optionally, one HA light entity per segment group. One E1 frame is sent per change, batched with a short debounce, because the lamp commits only one long frame per state round trip ([R] `docs/DX23012_PROTOCOL_FINDINGS.md:127-129`).

### 6.2 Colour profile (RGB vs RGBIC vs W / CCT) and wiring

- **IC type and colour order** are device settings. They are read with `get_symp_config_v2` and written with `set_wiring_v2` (fields `wiring` 4–6 and `sort` 1–3), previewed with `set_wiring_preview`, and calibrated with `buildLedCountCalibrationHEXCommand` [D].
- **The app does not re-assert the profile before each colour command.** It reads it when the device page opens and uses it to:
  - choose the segment count
  - choose whether W/CW channels exist (`icType` 5 = RGBW, 9 = RGBCW)
  - decide which UI to show
- **Mode switching is implicit.** An `E1 03` / colour command puts the device in colour mode, and `3b b1` puts it in white mode. The mode is reflected in the `ea 81` white-mode field ([R] `core/protocol.py:29-30`, `0x24`/`0x25`).
- **Recommendation:** read the profile and expose it. **Don't write wiring from Home Assistant by default.** A wrong `sort`/IC setting garbles the strip's output, and the app treats it as an installer setting.

### 6.3 Effects and scenes

- **Unified scenes:** `scene_data_v3` = `e0 02 {preview} {model 37–56} {speed 0–100} {bright 0–100}`, no checksum, cmdId 0x0B (inferred).
  - `preview` is 0 for "apply".
  - Confirmation arrives as an `ea 81` state frame with the mode byte set to the model.
  - **PID 170 doesn't list `scene_data_v3`.** Treat it as an experimental probe on this device.
- **PID 170's native effects** use the `hc3` functions: `symphony_scene_hc3` (built-in dynamic modes), `static_colorful_hc3`, `smear_hc3/_v2` (per-pixel paint), `custom_data_hc3` (user dynamic) and `music_data_hc3`.
  - All are `type: Hex` and built in Dart. **Their byte layouts are Unknown statically**, and the effect ID list for `symphony_scene_hc3` is not in the assets.
  - Recovering them needs either Dart AOT decompilation (blutter-class tooling, not available here) or an HCI/btsnoop capture of the app selecting effects. The repo already has a capture workflow ([R] `docs/CAPTURE_WORKFLOW.md`).

---

## 7. Implementation plan

Each phase is a separate change with tests.

1. **cmdId fix (D2, D10, D11).** Add a `command_family` per codec. Use 0x0B for every `iotbt-ea81` unified command: power, white, brightness and RGB.
   - Tests: the framed bytes for each command; `_send_inner` called with family 0x0B.
   - Hardware check: white and brightness on both lamps.
2. **Power to `switch_led_v3` (D1).** Add `iotbt_switch_led_v3(turn_on, preview=0, delay=0, gradient=0)` = `e0 01 00 3b {23|24} 00×5 00 00 00 00 00`.
   - Keep the old zone-power form as a fallback, behind a capability flag set after a successful state confirm.
   - Tests: golden bytes.
3. **Brightness via `bright_value_v2` (D11).** `3b 01 00 00 v 00 v 00 00 00 00 00` + cs. Use it only if the hardware retest passes; otherwise keep re-sending colour/white.
4. **Transport hardening (D4–D6).**
   - Serialise writes with an asyncio lock. Use `response=True` for multi-frame writes.
   - TX segmentation that follows §2.2.
   - In RX: parse ctrl, drop type-2 packets, reassemble continuation frames by index, and use `[4..5]` as the length.
   - Tests: encoder/decoder round trips using the APK layouts, including a segmented first frame plus continuation frames.
5. **Strip profile read (D9).** Send `63 12 21 f0`+cs after connecting, parse `pointCount`/`part`/`icType`/`sort`, and use `part` for E1 segment count (default 20).
   - Expose these as diagnostic attributes.
   - No wiring writes.
   - Tests: response parser fixture.
6. **Segment control (MIHEAL lamp).**
   - Generalise `iotbt_e1_segment_rgb` to `iotbt_e1_segments(records)`, taking one (hue, sat, bright) record per segment. The current single-colour function becomes a wrapper around it.
   - Cache per-segment colours in the client.
   - Add the `surplife_ble.set_segments` service: colour list or index map, segment count taken from phase 5 (default 20).
   - Optionally add per-segment-group light entities, with debounced batching into one frame.
   - Tests:
     - golden bytes for mixed records
     - the single-colour wrapper's output is unchanged
     - service schema validation
     - batching
   - Hardware check: a 20-colour gradient on both lamps, then a state readback.
7. **Effects.**
   - (a) Experimental `scene_data_v3` effect list (models 37–56), off by default, confirmed through the `ea 81` mode byte.
   - (b) Plan a btsnoop capture of the app selecting `symphony_scene_hc3` effects, speed and brightness to recover the hc3 layout. Then add an HA `effect_list` from it.

8. **Other lights from the app's device table (§8).** Add a product table keyed by state byte 4, generated from `ble_devices.json` + `wifi_dp_cmd.json`, and route commands by profile. Start with tier A; add tier B behind an "unverified" flag.

## 8. Supporting more lights from the app's data

The device table is data, not code, so much of it can be reused:
- `ble_devices.json` lists 62 BLE product IDs, each with category, protocols, state protocol and functions (with minimum firmware).
- `wifi_dp_cmd.json` gives byte templates for every `Json`-type function: power, brightness, CCT, scenes, timers, queries.
- The product ID is byte 4 of the `ea 81` state frame, which the repo already reads as `model_num`.

| Tier | Products (product ID) | Same as PID 170? | What works without new reverse engineering |
|---|---|---|---|
| **A** | `hc3` + `symphonyE1` + `wifibleSymphony`: **170, 171, 175, 57, 181, 182, 183, 186, 69, 76, 187, 184, 188, 86, 83** (15) | Identical protocol set | Everything this integration does for the lamp: E1 segments, `3bb1` white, `switch_led_v3`, the IC config query. Pixel and segment counts are read, not assumed. |
| **B** | `dj` / `dsdj` / `swdj` / `pmzk` bulbs and controllers on `common2_0` with `wifibleLightStandardV2` / `wifibleCCT` / `wifibleBrightness`: 53, 14, 30, 41, 62, 77, 85, 8, 60, 6, 72, 51, 7, 33, 65, 98, 81 | Same transport; no E1 | Power, brightness and CCT from templates. RGB through `3b a1` (the repo's `iotbt_3b_rgb`), which is *inferred* to be `colour_data_v2`. The state layout differs (`LightStandardV2`) and needs one capture per product family to confirm. |
| **C** | Older `common`/`common1_0`-only devices (68, 84, 9, 28, 22, 54, 55, 70, 23, 225, 226), classic-81 state | Older protocol | The repo's classic codec already covers the 0x71/0x31 form. Product IDs can be recognised and mapped. |
| **D** | Curtain lights (`curtainLightE2`), Christmas, 8-port screen controllers, music box, switch (151) | Different | Out of scope. They use Dart-built Hex commands, so each needs captures. |

Feasibility:
- **Tier A is cheap:** almost a table lookup once phases 1–6 land.
- **Tier B is moderate:** the templates exist, but each state parser needs a fixture from a real device.
- The data comes from untrusted app assets, so generate a checked-in Python table with a script and review it. Don't load the APK JSON at runtime.

Open questions, which only a capture or the hardware can settle:
- Is `{open}` really `23`/`24`?
- Does PID 170 accept `e0 02`?
- What are the `symphony_scene_hc3` bytes?
- Does the app send light commands with or without response?

## 9. Implementation status (2026-11-20)

Phases 1–6 and tier A are implemented. All are covered by unit tests; none is verified on hardware yet.

| Area | Where | Notes |
|---|---|---|
| cmdId 0x0B for power, white and E1 | `core/client.py`, `IOTBT_COMMAND_FAMILY` | The state query and profile query still try 0x0A first. |
| `switch_led_v3` power | `iotbt_power`, `LightClient._set_iotbt_power` | Sends the app form, confirms with a state query, falls back to the legacy zone form, and caches whichever worked. |
| Segmented TX | `framing.segment_magichome` | Used only when the client reports a negotiated MTU above 23. Max frame = MTU − 3, capped at 255. Each segment is a write-with-response. |
| RX: segments and acks | `NotificationReassembler` | Reassembles segmented notifications and drops ACK_PACK frames. |
| Strip profile | `SYMPHONY_CONFIG_QUERY`, `parse_symphony_config`, `LightClient.query_strip_profile` | The coordinator asks once per entry. The result sets `segment_count`; the default is 20. |
| Per-segment colour | `iotbt_e1_segments`, `LightClient.set_segments`, the `surplife_ble.set_segments` entity service, CLI `segments` | A shorter list is stretched evenly. Brightness changes re-send the pattern. A uniform colour or white clears it. |
| Product table | `core/products.py` | 62 products. Tier A = 57, 69, 76, 83, 86, 170, 171, 175, 181, 183, 184, 186, 187, 188. 182 is excluded because the flux_led reference maps it to extended-custom. Classification still requires the verified EA 81 layout (`byte 5 == 0x09`), so tier A only adds naming and the `segments` capability. |

Hardware checks still open: whether 0x0B power and white are accepted, whether the lamp answers the profile query (and on which cmdId), and whether segmented TX works at the lamp's real MTU.
