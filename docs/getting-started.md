# Installation & Configuration

*Previous: [← Overview](overview.md) | Next: [How It Works →](how-it-works.md)*

How to install ANS via HACS or manually, configure the integration, and set up your first recipients.

## Contents

- [Installation](#installation)
  - [Via HACS (Recommended)](#via-hacs-recommended)
  - [Manual Installation](#manual-installation)
- [Initial Setup](#initial-setup)
  - [System Settings](#system-settings)
- [Options (Runtime Tuning)](#options-runtime-tuning)
- [Adding Recipients](#adding-recipients)
  - [Step 1 — Recipient Type](#step-1--recipient-type)
  - [Step 2 — Recipient Details](#step-2--recipient-details)
  - [Step 3 — Basic Settings](#step-3--basic-settings)
  - [Step 4 — TTS Settings](#step-4--tts-settings-text-to-speech-recipients-only)
  - [Step 5 — Channel Mapping](#step-5--channel-mapping)
  - [Step 6 — Do Not Disturb](#step-6--do-not-disturb)
- [Reconfiguring](#reconfiguring)

## Installation

### Via HACS (Recommended)

1. Open HACS in your Home Assistant sidebar.
2. Go to **Integrations** and click the three-dot menu (⋮) in the top right.
3. Select **Custom repositories**.
4. Enter `https://github.com/txxa/hass-ans` as the repository URL and select **Integration** as the category.
5. Click **Add**.
6. Search for **"Advanced Notification System"** in HACS and click **Download**.
7. Restart Home Assistant.

### Manual Installation

1. Download `ans.zip` from the [latest release](https://github.com/txxa/hass-ans/releases).
2. Extract the archive. It contains a folder named `ans/`.
3. Copy the `ans/` folder into your Home Assistant `custom_components/` directory:
   ```
   config/
   └── custom_components/
       └── ans/
           ├── __init__.py
           ├── manifest.json
           └── ...
   ```
4. Restart Home Assistant.

---

## Initial Setup

After restarting, add the integration:

1. Go to **Settings → Devices & Services → Integrations**.
2. Click **+ Add Integration** and search for **"Advanced Notification System"**.
3. The setup wizard opens with one configuration step.

### System Settings

| Field | Description | Default |
|---|---|---|
| **Enabled notification channels** | Channels available system-wide. Only enabled channels can be assigned to recipients. Detected `notify.*` services appear automatically. `notify.persistent_notification` is pre-selected by default. Media player entities appear only when a TTS service is configured. | `notify.persistent_notification` |
| **Text-to-Speech service** | The TTS integration to use for TTS recipients (e.g., `tts.google_translate_say`). Leave empty if you don't plan to use TTS recipients. Only shown when a TTS integration is detected. | None |
| **Enable audit logging** | Records all notifications and delivery attempts to disk. Disable to reduce storage usage. Retention period is set in Options. | On |

> **Tip:** If a channel you expect (e.g., `notify.mobile_app_*` or a media player) is not showing in the channel list, confirm the underlying integration is running, then come back and reconfigure once channels are detected. You can also add channels later via **Reconfigure**.

Click **Submit** to complete setup.

---

## Options (Runtime Tuning)

After initial setup you can adjust system-wide performance settings at any time without reinstalling. Go to **Settings → Devices & Services → Integrations → Advanced Notification System → Configure**.

| Field | Description | Default | Range |
|---|---|---|---|
| **Global rate limit** | Max notifications per minute system-wide. Set to `0` to disable. | 100 | 0–10 000 |
| **Retry base delay** | Seconds before the first retry after a transient failure. | 60 | 1–3 600 |
| **Retry backoff multiplier** | Each successive retry waits this many times longer than the previous. `2.0` doubles the wait each time. | 2 | 1.0–5.0 |
| **Retry maximum delay** | Upper cap on the delay between retries regardless of backoff. | 3 600 s (1 hr) | 60–86 400 |
| **Queue max concurrency** | Number of delivery tasks that run in parallel. Higher values increase throughput but consume more resources. | 5 | 1–20 |
| **Audit log retention** | Days to keep notification and delivery records. Set to `0` to disable automatic cleanup. Only visible when audit logging is on. | 7 | 0–365 |

---

## Adding Recipients

Recipients are configured as **sub-entries** under the main ANS integration entry. Each recipient is a person, device, or target that can receive notifications.

To add a recipient: **Settings → Devices & Services → Advanced Notification System → Add Recipient**.

### Step 1 — Recipient Type

Choose the type of recipient:

| Type | Description |
|---|---|
| **Home Assistant (System)** | Routes to the HA persistent notification sidebar. Can only be added once. |
| **Home Assistant User** | Linked to an existing HA user account. Used primarily for Mobile App push notifications. |
| **Text-to-Speech recipient** | Delivers spoken audio via a media player. Requires a TTS service to be configured at the system level. |
| **Generic recipient** | A custom-named recipient with optional email and phone number. Use for Signal or any channel that requires contact details. |

> **Note:** Only available types are shown. **Home Assistant (System)** disappears once added. **Home Assistant User** is hidden when all HA users are already configured. **Text-to-Speech recipient** only appears when a TTS service is configured at the system level.

### Step 2 — Recipient Details

> **Note:** This step is skipped for **Home Assistant (System)** — it is fully automatic with a fixed name and pre-configured channels.

**For Home Assistant User:**
- **Home Assistant user** — select the HA user account to link. The display name is auto-filled from the selected account.
- **Email address** (optional) — required for email-based channels (reserved for future use)
- **Phone number** (optional) — required for phone-based channels (e.g. Signal). Must be in [E.164 format](https://en.wikipedia.org/wiki/E.164) (e.g., `+1234567890`)

**For Generic recipient:**
- **Display name** — unique name for the recipient
- **Email address** (optional) — required for email-based channels (reserved for future use)
- **Phone number** (optional) — required for phone-based channels (e.g. Signal). Must be in [E.164 format](https://en.wikipedia.org/wiki/E.164) (e.g., `+1234567890`)

**For Text-to-Speech recipient:**
- **Display name** — unique name for the recipient

### Step 3 — Basic Settings

| Field | Description | Default |
|---|---|---|
| **Rate limit** | Max notifications per minute for this recipient. Set to `0` to disable. | 20 |
| **Retry attempts** | Retries on transient failure for this recipient. Set to `0` to disable. | 3 |
| **Notification types to receive** | Only selected types are delivered. All others are silently filtered. | All types |
| **Blocked sources (regex)** | Notifications whose `source` matches this pattern are blocked. Example: `^automation\.test_` blocks all test automations. | None |

### Step 4 — TTS Settings *(Text-to-Speech recipients only)*

| Field | Description | Default |
|---|---|---|
| **Message Format** | `Title and Message` / `Message Only` / `Title Only` | Title and Message |
| **Enable SSML Mode** | Wraps message in SSML `<speak>` tags. Only enable for SSML-capable TTS engines (Google Cloud TTS, Piper in SSML mode, etc.). | Off |
| **Enable Volume Management** | ANS adjusts and restores media player volume automatically. Disable if you manage volume externally. | On |
| **Morning Volume (6:00–9:00)** | Playback volume level 0–100 | 65 |
| **Daytime Volume (9:00–19:00)** | Playback volume level 0–100 | 75 |
| **Evening Volume (19:00–22:00)** | Playback volume level 0–100 | 60 |
| **Night Volume (22:00–6:00)** | Playback volume level 0–100 | 50 |
| **Override Volume Level** | Volume used when a notification's criticality matches the override list | 80 |
| **Override for Criticalities** | Criticalities that use the override volume instead of time-based volume | None |

### Step 5 — Channel Mapping

Map each criticality level to the channels that should receive notifications at that level. Channels available in the list are those enabled system-wide and depend on the recipient type:

- **Home Assistant (System):** `notify.persistent_notification` only
- **Home Assistant User / Generic recipient:** `notify.*` services (e.g. `notify.mobile_app_phone`, `notify.signal`)
- **Text-to-Speech recipient:** media player entities (e.g. `media_player.living_room_speaker`)

| Field | Description |
|---|---|
| **Channels for LOW criticality** | Leave empty to suppress low-criticality notifications entirely |
| **Channels for MEDIUM criticality** | |
| **Channels for HIGH criticality** | |
| **Channels for CRITICAL criticality** | Strongly recommended to always have at least one channel here |

**Example mapping for an HA User recipient:**

| Criticality | Channels |
|---|---|
| LOW | — *(suppressed)* |
| MEDIUM | `notify.mobile_app_my_phone` |
| HIGH | `notify.mobile_app_my_phone` |
| CRITICAL | `notify.mobile_app_my_phone`, `notify.signal` |

### Step 6 — Do Not Disturb

| Field | Description | Default |
|---|---|---|
| **Enable Do Not Disturb** | Enables quiet hours for this recipient | Off |
| **DND start time** | When the quiet period begins (24-hour, e.g. `22:00`) | 22:00 |
| **DND end time** | When the quiet period ends. Can cross midnight (e.g. `06:00`). | 06:00 |
| **Allowed sources during DND (regex)** | Notifications from matching sources always bypass DND | None |
| **Criticality levels that bypass DND** | Select levels that always get through regardless of quiet hours | `CRITICAL` |
| **Notification types that bypass DND** | Select types that always get through | `ALERT` |

> **Example:** DND from 22:00 to 06:00, with `CRITICAL` criticality and `SECURITY` type bypassing. A midnight smoke alarm (`SECURITY`, `CRITICAL`) gets through. A medication reminder (`REMINDER`, `MEDIUM`) does not.

---

## Reconfiguring

To change system settings or any recipient's configuration:

- **System settings:** Three-dot menu (⋮) on the integration card → **Reconfigure**
- **Options (rate limits, retry, queue):** Integration card → **Configure**
- **A recipient:** Sub-entry list → select the recipient → **Reconfigure**

All reconfiguration takes effect immediately without restarting Home Assistant.

---

*Previous: [← Overview](overview.md) | Next: [How It Works →](how-it-works.md)*
