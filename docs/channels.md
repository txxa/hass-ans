# Channel Reference

*Previous: [← How It Works](how-it-works.md) | Next: [Usage Examples →](usage-examples.md)*

Complete reference for every built-in channel adapter: delivery behavior, field handling, `channel_data` options, acknowledgement mechanics, and limitations.

## Contents

- [Persistent Notification](#persistent-notification)
  - [Delivery Behavior](#delivery-behavior-persistent-notification)
  - [Field Handling](#field-handling-persistent-notification)
  - [Acknowledgement](#acknowledgement-persistent-notification)
  - [Limitations](#limitations-persistent-notification)
- [Mobile App](#mobile-app)
  - [Delivery Behavior](#delivery-behavior-mobile-app)
  - [Field Handling](#field-handling-mobile-app)
  - [Actions Reference](#actions-reference)
  - [`data.tag` and Acknowledgement Tracking](#datatag-and-acknowledgement-tracking)
  - [`channel_data` Reference](#channel_data-reference-mobile-app)
  - [Limitations](#limitations-mobile-app)
- [Signal Messenger](#signal-messenger)
  - [Delivery Behavior](#delivery-behavior-signal)
  - [Field Handling](#field-handling-signal)
  - [`channel_data` Reference](#channel_data-reference-signal)
  - [Attachment Path Restriction](#attachment-path-restriction)
  - [Limitations](#limitations-signal)
- [TTS via Media Player](#tts-via-media-player)
  - [Delivery Behavior](#delivery-behavior-tts)
  - [Message Format](#message-format)
  - [Volume Management](#volume-management)
  - [Volume Restoration Registry](#volume-restoration-registry)
  - [SSML Mode](#ssml-mode)
  - [Device State Handling](#device-state-handling)
  - [Limitations](#limitations-tts)

---

## Persistent Notification

Delivers notifications to the HA frontend sidebar via `persistent_notification.create`. Always available — no external integration required.

> **Note:** `notify.persistent_notification` can only be assigned to the **HA System** (`Home Assistant (System)`) recipient type. It is not selectable for HA User, Generic, or TTS recipients.

### Delivery Behavior {#delivery-behavior-persistent-notification}

ANS calls `persistent_notification.create` with a rendered `message` body. The notification_id is embedded so ANS can correlate a sidebar dismissal back to the original ANS notification for acknowledgement tracking.

### Field Handling {#field-handling-persistent-notification}

| Field | Rendering in sidebar |
|---|---|
| `title` | Notification title |
| `message` | Notification body |
| `image` (http/https URL) | Clickable Markdown link: `[filename.jpg](https://…)` |
| `image` (local path, e.g. `/local/img.jpg`) | Inline Markdown image embed: `![image](/local/img.jpg)` |
| `video` (http/https or local path) | Clickable Markdown link: `[filename.mp4](https://…)` |
| `file` (http/https or local path) | Clickable Markdown link: `[filename.pdf](https://…)` |
| `link` | Rendered as `[Details](url)` |
| `context` | Appended as a `Context:` section; values matching a known HA entity ID are auto-linked to the entity history page |
| `actions` | Silently ignored |
| `channel_data` | Silently ignored |

**URL validation:** `image`, `video`, and `file` values that have no filename path segment (e.g. `https://example.com` with no path) are silently skipped and a WARNING is written to the HA log.

### Acknowledgement {#acknowledgement-persistent-notification}

Dismissing a persistent notification from the HA sidebar counts as an acknowledgement. ANS listens for `SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED` with `UpdateType.REMOVED` via `async_dispatcher_connect` and matches the dismissed notification's ID to a pending ANS delivery. When a match is found, ANS fires `ans_notification_acknowledged` and records the acknowledgement in the `AcknowledgementRegistry`.

### Limitations {#limitations-persistent-notification}

- No push/sound — passive, sidebar-only delivery.
- `actions`, `channel_data`, `video` (non-http), and TTS-style fields are not forwarded.
- Exclusive to the HA System recipient type.

---

## Mobile App

Delivers push notifications via `notify.mobile_app_{device_id}` using the HA Companion App.

**Prerequisite:** The [HA Companion App](https://companion.home-assistant.io/) must be installed and logged in on the target device.

### Delivery Behavior {#delivery-behavior-mobile-app}

ANS calls `notify.mobile_app_{device_id}` with `title`, `message`, and a `data:` dict assembled from the notification fields and any `channel_data` overrides. The `data:` dict is built as follows:

1. `channel_data` is flat-merged as the base
2. `image`, `link`, `context.entity`, and `actions` are conditionally added on top
3. `data.tag` is always set last (to the `notification_id` UUID, or to `channel_data.tag` when provided)

### Field Handling {#field-handling-mobile-app}

| Field | Behavior |
|---|---|
| `title` | Forwarded as-is |
| `message` | Forwarded as-is |
| `image` (http/https with filename path) | Forwarded as `data.image` for inline push display |
| `image` (bare-domain URL or local path) | Silently ignored with a warning log |
| `video`, `file` | Silently ignored |
| `link` | Forwarded as `data.url` (iOS/macOS) and `data.clickAction` (Android) |
| `context.entity` | When `link` is not set, used as tap-action deep-link: `entityId:<entity_id>` |
| `context` (other keys) | Silently ignored |
| `actions` | Forwarded as `data.actions` (up to 3 buttons) |
| `channel_data` | Flat-merged into the `data:` payload |

### Actions Reference

Action buttons are defined in the top-level `actions` field of `ans.send_notification` (not under `channel_data`). ANS forwards them to `notify.mobile_app_*` as the `actions` key inside the `data:` payload.

| Key | Required | Type | Description |
|---|---|---|---|
| `action` | Yes | `str` | Identifier returned in the `mobile_app_notification_action` event when the button is tapped. Must be a non-empty string. Convention: use uppercase with underscores (e.g. `CLOSE_GARAGE`). |
| `title` | Yes | `str` | Button label displayed on the notification. |
| `uri` | No | `str` | URI to open when the button is tapped. Accepts any URI scheme the Companion App supports: `https://`, `homeassistant://`, deep links, etc. |

Additional Companion App keys (e.g. `destructive: true` for iOS red buttons) can be included alongside these and are forwarded as-is.

**Limits:** Maximum **3** action buttons per notification, enforced by ANS before delivery.

**Receiving the action event:**

```yaml
- alias: "Handle tapped action"
  trigger:
    - platform: event
      event_type: mobile_app_notification_action
      event_data:
        action: "CLOSE_GARAGE"   # matches the 'action' key you defined
  action:
    - service: cover.close_cover
      target:
        entity_id: cover.garage_door
```

The response automation is standard Home Assistant — ANS is not involved after the notification is delivered.

### `data.tag` and Acknowledgement Tracking

ANS always sets `data.tag` on every Mobile App notification. The tag defaults to the `notification_id` UUID; when `channel_data.tag` is provided, that value is used instead and ANS records it at delivery time to maintain correlation.

The HA Companion App includes the tag in every `mobile_app_notification_action` event it fires, regardless of which button was tapped. ANS listens for this event, matches the tag against pending deliveries, and — when a match is found — fires `ans_notification_acknowledged` and records the acknowledgement in the `AcknowledgementRegistry`.

For mobile acknowledgements, `ans_notification_acknowledged` includes the standard fields (`notification_id`, `channel_id`, `acknowledged_at`) and additionally:

- `method` — `"action_button"` when an action button was tapped; `"notification_tap"` when the notification body was tapped
- `action` — the action identifier from `mobile_app_notification_action` (only present when `method` is `"action_button"`)
- `device_name` — the device slug derived from the delivery channel (e.g. `"sm_g930f"` from `notify.mobile_app_sm_g930f`)

The HA event `context.user_id` carries the HA user who interacted with the notification, following standard HA conventions.

For persistent notification acknowledgements, `method` is `"persistent_notification_dismiss"` and `action`/`device_name` are omitted.

> **Note:** `data.tag` is reserved for acknowledgement tracking. To control the tag for notification grouping or replacement, supply `tag` in `channel_data` — ANS will use that value for both grouping and ack correlation.

```yaml
service: ans.send_notification
data:
  source: "automation.ha_update"
  title: "Update Ready"
  message: "Home Assistant 2026.5.0 is available."
  type: INFO
  criticality: MEDIUM
  channel_data:
    tag: "ha_update"   # Custom tag for notification grouping/replacement and ack tracking
```

### `channel_data` Reference {#channel_data-reference-mobile-app}

All keys in `channel_data` are flat-merged into the `data:` payload sent to `notify.mobile_app_*`. This means all standard [HA Companion App notification features](https://companion.home-assistant.io/docs/notifications/notifications-basic/) work via `channel_data`:

| Key | Type | Description |
|---|---|---|
| `tag` | `str` | Overrides the default `notification_id` UUID tag. Used for notification grouping, replacement, and ANS ack correlation. |
| `channel` | `str` | Android notification channel (controls sound/vibration profile). |
| `importance` | `str` | Android importance level (`high`, `low`, `default`, etc.). |
| Any Companion App key | varies | All other keys are forwarded as-is. |

### Limitations {#limitations-mobile-app}

- `video`, `file`, `context` (non-entity keys), Signal-specific `channel_data` keys — all silently ignored.
- `image` only works with http/https URLs that contain a filename path segment. Local paths are not forwarded.
- Acknowledgement eligibility is persisted by ANS. Notifications delivered before an HA restart can still emit `ans_notification_acknowledged` after restart when the user taps an action.
- Action buttons are silently ignored by all other channel adapters (Signal, Persistent Notification, TTS).

---

## Signal Messenger

Delivers messages via `notify.signal` using the HA Signal integration.

**Prerequisite:** The [Signal integration](https://www.home-assistant.io/integrations/signal_messenger/) must be configured in HA with a running Signal API server. The recipient must have a phone number in [E.164 format](https://en.wikipedia.org/wiki/E.164) (e.g. `+1234567890`).

### Delivery Behavior {#delivery-behavior-signal}

ANS calls `notify.signal` with the recipient's phone number as the target. When `text_mode: styled` is set in `channel_data` (or when a title is present without an explicit mode), the message is formatted with a bold title (`**Title**\nMessage`).

### Field Handling {#field-handling-signal}

| Field | Behavior |
|---|---|
| `title` | Prepended to the message as `**bold**` text when `text_mode: styled` (or when no mode is set and a title is present) |
| `message` | Forwarded as the message body |
| `image` (http/https with filename path segment) | Added to the `urls` list |
| `image` (local path) | Added to the `attachments` list |
| `video` (http/https with filename path segment) | Added to the `urls` list |
| `video` (local path) | Added to the `attachments` list |
| `file` (http/https with filename path segment) | Added to the `urls` list |
| `file` (local path) | Added to the `attachments` list |
| `link` | Appended as plain text to the message body |
| `context` | Silently ignored |
| `actions` | Silently ignored |
| `image`/`video`/`file` (bare-domain URLs) | Silently ignored with a warning log |

### `channel_data` Reference {#channel_data-reference-signal}

| Key | Type | Description |
|---|---|---|
| `text_mode` | `"styled"` \| `"normal"` | `styled` uses Signal's markdown-like formatting (`**bold**`, `_italic_`). When not set and a title is present, ANS automatically uses `styled`. |
| `attachments` | `list[str]` | Additional local file paths to attach. Subject to the path restriction below. |
| `urls` | `list[str]` | Additional image URLs to send as attachments. Unlike the top-level `image`/`video`/`file` fields, URLs in this list are **not** validated for a filename path segment — for intentionally bare-domain URLs (advanced use only). |
| `verify_ssl` | `bool` | Whether to verify SSL certificates when fetching image URLs. Default: `true`. Set to `false` for self-signed certificates. |

### Attachment Path Restriction

ANS validates every path in `attachments` before forwarding it to Signal. Only paths that resolve to a location inside one of the following HA directories are allowed:

- `config/` (the HA configuration root)
- `config/media/`
- `config/www/`

Paths outside these directories — including traversal attempts (`../../etc/passwd`) and symlinks that point outside the allowed tree — are silently dropped and a warning is written to the HA log. The notification is still delivered with any remaining valid attachments. If all paths are invalid, the notification is sent without attachments.

This restriction also applies to the top-level `image`, `video`, and `file` fields when they contain local paths.

> **Note:** Phone numbers are logged only by last 4 digits at debug level (`****1234`). Full numbers are never written to logs.

### Limitations {#limitations-signal}

- No acknowledgement tracking — Signal has no interaction model ANS can observe.
- `context`, `actions`, and Mobile App-specific `channel_data` keys are silently ignored.
- Bare-domain URLs (no filename path segment) in the top-level `image`/`video`/`file` fields are silently ignored; add them explicitly via `channel_data.urls` if needed.

---

## TTS via Media Player

Delivers spoken announcements via a configured TTS integration targeting a `media_player.*` entity.

**Prerequisites:** A TTS integration (e.g. [Google Translate TTS](https://www.home-assistant.io/integrations/google_translate/), [Piper](https://www.home-assistant.io/integrations/wyoming/)) and at least one media player entity that supports both `PLAY_MEDIA` and `VOLUME_SET`.

### Delivery Behavior {#delivery-behavior-tts}

ANS calls the configured TTS service (e.g. `tts.google_translate_say`) targeting the media player entity. The text spoken is determined by the TTS recipient's **Message Format** setting.

A per-device delivery lock serializes concurrent TTS requests to the same media player — only one TTS task runs at a time per entity.

### Message Format

Configured per TTS recipient in Step 4 of the recipient wizard:

| Setting | Spoken output |
|---|---|
| `Title and Message` (default) | `"{title}. {message}"` |
| `Message Only` | `"{message}"` |
| `Title Only` | `"{title}"` |

> **Tip:** Use `Message Only` for announcements where the title would be redundant or awkward when spoken. With the default `Title and Message` format, passing an empty `title: ""` produces a leading period in the spoken text.

### Volume Management

When **Enable Volume Management** is on (the default), ANS performs a capture-set-speak-restore cycle:

1. **Capture** the current media player volume
2. **Set** volume to the time-of-day level (or criticality override level)
3. **Speak** the TTS message
4. **Restore** the original volume when the player reaches `IDLE` state

**Time-of-day volume windows** (boundaries are fixed; volumes are configurable per recipient):

| Window | Time |
|---|---|
| Morning | 06:00 – 09:00 |
| Daytime | 09:00 – 19:00 |
| Evening | 19:00 – 22:00 |
| Night | 22:00 – 06:00 |

**Criticality override:** A separate override volume can be configured along with a list of criticalities that trigger it, regardless of time of day (e.g. `CRITICAL` always plays at 90).

### Volume Restoration Registry

The restoration registry (`persistence/volume_restoration.py`) handles edge cases in the capture-restore lifecycle:

- **Persisted across restarts** — state is stored under the `ans.volume_restoration` key in HA storage. If HA restarts between the TTS call and the IDLE event, restoration is still attempted on the next startup.
- **User-change detection** — if the volume is manually adjusted within 5 seconds of ANS setting it (echo-guard window), ANS treats it as intentional and aborts restoration to avoid overriding user intent.
- **Timeout expiry** — restoration intents expire after 1 hour. If a media player never reaches IDLE within that window, the intent is discarded.
- **120-second fallback** — a safety timer fires 120 seconds after playback starts as a fallback for players that don't reliably signal IDLE.
- **Per-device lock** — the async delivery lock has a 60-second acquisition timeout to prevent deadlock if a previous delivery hung.

### SSML Mode

When **Enable SSML Mode** is on in TTS recipient settings, ANS wraps the message in an SSML `<speak>` document and XML-escapes all user content before sending. This enables prosody markup, pauses, and emphasis in TTS engines that support SSML.

**Enable only for SSML-capable engines:**
- Google Cloud TTS
- Amazon Polly
- Azure TTS
- edge-tts
- Piper (in SSML mode)

Do **not** enable for plain-text engines (Google Translate TTS, built-in macOS/Windows TTS via HA). Plain-text engines will speak the raw XML tags aloud.

### Device State Handling

| Device state | ANS behavior |
|---|---|
| Normal (playing/idle/paused) | Full deliver with volume management |
| Standby (`off`) | Deliver without volume management. Platforms like Google Cast handle wakeup natively when they receive a play command. Volume management is skipped because a standby device may not respond to `volume_set`. No capture/restore intent is created. |
| Unavailable (`unavailable`) | Skip delivery; schedule a retry with exponential backoff |

### Limitations {#limitations-tts}

- No acknowledgement tracking — spoken audio has no interaction model ANS can observe.
- Only `title` and `message` are spoken; `image`, `video`, `file`, `link`, `context`, `actions`, and `channel_data` are silently ignored.
- Volume window boundaries (06:00, 09:00, 19:00, 22:00) are fixed and not user-configurable; only the volume levels at each window are configurable.
- Standby (`off`) devices skip volume management entirely — announcements play at the device's own wake-up volume level.

---

*Previous: [← How It Works](how-it-works.md) | Next: [Usage Examples →](usage-examples.md)*
