# Home Assistant setup

## Repo name suggestion

**`ha-surplife-ble`** — short, matches the domain, clearly identifies the app ecosystem.

Alternatives if taken:
- `surplife-magichome2-ble`
- `dx23012-ble`
- `magichome2-ble-light`

## 1. Create the GitHub repo

```bash
cd /Users/nathanbland/projects/ble-rgb-control
git init
git add -A
git commit -m "Initial commit: Surplife BLE toolkit and HA integration"
gh repo create ha-surplife-ble --public --source=. --remote=origin --push
```

## 2. Install in Home Assistant (manual)

1. Copy the `custom_components/surplife_ble/` folder into your HA config:

   ```
   config/custom_components/surplife_ble/
   ```

   The folder should contain `__init__.py`, `manifest.json`, `light.py`,
   `config_flow.py`, `coordinator.py`, `const.py`, `strings.json`,
   `translations/`, and the `core/` subdirectory.

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration**.

4. Search for **"Surplife BLE"** and select it.

5. Follow the config flow to pair your device (enter the BLE address or
   select from discovered devices).

## 3. Make it HACS-installable

HACS requires a few specific files. Add these to the repo root:

### `hacs.json`

```json
{
  "name": "Surplife BLE",
  "content_in_root": false,
  "render_readme": true,
  "homeassistant": "2024.1.0",
  "country": ["US"],
  "domains": ["light"],
  "iot_class": "local_polling",
  "integration_type": "device"
}
```

### Update `manifest.json`

Set `codeowners` and `documentation` to your GitHub repo:

```json
{
  "domain": "surplife_ble",
  "name": "Surplife BLE",
  "codeowners": ["@NathanBland"],
  "config_flow": true,
  "dependencies": ["bluetooth_adapters"],
  "documentation": "https://github.com/NathanBland/ha-surplife-ble",
  "integration_type": "device",
  "iot_class": "local_polling",
  "requirements": []
}
```

### Add `README.md` section for HACS

Add a section like:

```markdown
## Installation via HACS

1. Add this repo as a custom repository in HACS (type: Integration).
2. Install "Surplife BLE" from HACS.
3. Restart Home Assistant.
4. Add the integration via Settings → Devices & Services.
```

## 4. Submit to HACS default store (optional, later)

Once the integration is stable and tested:

1. Fork [hacs/default](https://github.com/hacs/default)
2. Add your repo to `integrations` in the appropriate category
3. Submit a PR

HACS review criteria typically require:
- Working config flow
- Proper manifest.json
- README with installation instructions
- Active maintenance

## 5. Testing on Home Assistant

### Prerequisites

- Home Assistant 2024.1+ (for BLE entity support)
- A Bluetooth adapter connected to HA (built-in, USB, or ESPHome proxy)
- The DX23012 lamp powered on and in BLE range

### What works

| Control | Status |
|---------|--------|
| Power on/off | Verified — E0 command, state updates |
| White brightness | Verified — 3B B1 command, state updates |
| RGB color | Verified — 3B A1 command, physical output changes |
| Brightness (color mode) | Works — resends current color with new brightness |
| Brightness (white mode) | Works — resends white with new brightness |

### Known limitations

- RGB color changes do not update the device's EA81 state frame. The HA
  entity uses `assumed_state` so the UI reflects what was commanded, not
  what the device reports. This is a firmware limitation — the Android app
  uses a JSON state-frame write to FF02 that only Android's BLE stack allows.
- The device must be the sole controller of the lamp for state to remain
  consistent. If the physical button or another app changes the lamp, HA
  will not detect color changes (power and white brightness will still
  sync on the next poll).
