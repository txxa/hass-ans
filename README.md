# Advanced Notification System (ANS) for Home Assistant

[![GitHub Release](https://img.shields.io/github/release/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/commits/main)
[![License](https://img.shields.io/github/license/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)

_A sophisticated notification management system for Home Assistant with advanced routing, filtering, and delivery control._

ANS is a custom Home Assistant integration that acts as a centralized notification hub. Instead of calling `notify.mobile_app_*`, `notify.signal`, or TTS services individually from each automation, you make a single `ans.send_notification` call and ANS handles the rest: routing to the right recipients, choosing the right channels based on criticality, applying Do Not Disturb schedules, rate limiting, retrying on failure, and managing TTS volume.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [Troubleshooting](#troubleshooting)
- [Advanced Topics](#advanced-topics)
- [Development and Maintenance](#development-and-maintenance)
- [Contributions](#contributions)

## Features

- **Single service call** routes to all configured recipients and channels simultaneously
- **Criticality-based channel mapping** — LOW through CRITICAL each trigger different channels per recipient
- **Type and criticality filtering** per recipient (INFO, WARNING, ALERT, REMINDER, EVENT, SECURITY)
- **Do Not Disturb** per recipient with configurable time windows and bypass rules for type and criticality
- **Source blocking** via per-recipient regex patterns
- **Rate limiting** — token bucket algorithm at both per-recipient and global level, with automatic retry queuing
- **Queue depth limit** — configurable bounded delivery queue (default 500 tasks) prevents memory exhaustion under automation storms; excess tasks are dropped with a warning and a `ans_notification_failed` event
- **Retry with exponential backoff** — configurable attempts and delays, with crash recovery across HA restarts
- **Deduplication** — idempotent delivery prevents duplicate notifications, including duplicate TTS playback
- **TTS via media player** — time-based and criticality-based volume control, automatic volume restoration, and per-device delivery lock to prevent overlapping playback
- **Mobile app, Signal Messenger, persistent notification** support out of the box; Signal attachments are restricted to HA-managed directories (`config/`, `media/`, `www/`) to prevent path-traversal access; media URLs without a filename path segment (e.g. bare domains) are silently ignored with a warning log across all adapters; persistent notification image/video/file links use the filename as the label; extensible adapter architecture for additional channels
- **Actionable mobile notifications** — include up to 3 action buttons in mobile push notifications via the `actions` field; button taps fire a `mobile_app_notification_action` HA bus event for automation response
- **Acknowledgement tracking** — ANS tracks whether a delivered notification was acknowledged: tapping any action button on a mobile push, or dismissing a persistent notification in the HA sidebar, fires an `ans_notification_acknowledged` HA bus event and records the acknowledgement in a persistent `AcknowledgementRegistry`; automations can correlate the `notification_id` returned by `ans.send_notification` with this event to detect confirmed read receipts
- **Audit log** — notification registry and delivery attempt logs with configurable retention (default 7 days, max 365 days) and hourly auto-purge
- **Delivery outcome events** — `ans_notification_delivered`, `ans_notification_filtered`, `ans_notification_failed`, and `ans_notification_rate_limited` HA bus events let automations react to per-channel delivery results in real time
- **Notification settled event** — `ans_notification_settled` fires once all fan-out tasks for a notification reach a terminal state, carrying per-recipient channel counts and a `recipients_delivered` total so automations can detect total delivery failure
- **Stale channel Repairs issues** — when a `notify.*` service or `media_player.*` entity disappears from HA, ANS raises a HA Repairs issue in the UI with a clear remediation step; the issue is automatically dismissed when the channel recovers
- **Service response** — `ans.send_notification` returns `{"notification_id": "..."}` for use with `response_variable`, enabling event correlation in automations
- **Built-in diagnostics** panel for channel health and adapter status

For the full feature list, use cases, and requirements see [Overview](docs/overview.md).

## Installation

1. Add this repository as a custom repository to HACS: [![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=txxa&repository=hass-ans&category=integration)
2. Use HACS to install the integration.
3. Restart Home Assistant.
4. Set up the integration using the UI: [![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ans)

For manual installation see [Getting Started](docs/getting-started.md).

## Configuration

### Configuring the Integration

To add the integration, go to **Settings ➔ Devices & Services ➔ Integrations**, click ➕ Add Integration, and search for "Advanced Notification System" or "ANS".

After installation, additional system-wide settings can be configured via the Options menu (**Settings ➔ Devices & Services ➔ Integrations ➔ Advanced Notification System ➔ Configure**).

The initial setup options (TTS Service, Enabled Channels, and Audit Logging) can be changed at any time using the Reconfigure flow (**Settings ➔ Devices & Services ➔ Integrations ➔ Advanced Notification System ➔ Reconfigure**).

### Adding a Recipient

1. Go to **Settings ➔ Devices & Services ➔ Integrations ➔ Advanced Notification System**
2. Click **Add Recipient**
3. Follow the configuration steps

For the full configuration walkthrough see [Getting Started](docs/getting-started.md).

## Quick Start

Once installed and at least one recipient is configured, send your first notification:

```yaml
service: ans.send_notification
data:
  source: "my_automation"
  title: "Hello from ANS"
  message: "ANS is working."
  type: INFO
  criticality: LOW
```

| Field | Required | Values |
|---|---|---|
| `source` | ✅ | any string — used for source-blocking filters |
| `title` | ✅ | any string |
| `message` | ✅ | any string |
| `type` | ✅ | `INFO` `WARNING` `ALERT` `REMINDER` `EVENT` `SECURITY` |
| `criticality` | ✅ | `LOW` `MEDIUM` `HIGH` `CRITICAL` |
| `image` | ❌ | http/https URL or HA-relative path (e.g. `/local/img.jpg`, `/api/camera_proxy/camera.front`). Persistent notification: http/https renders as a clickable filename link; local path renders as an inline Markdown image embed. Mobile app: forwarded as push image (http/https only). Signal: forwarded as URL or attachment. URLs without a file path segment are ignored with a warning. |
| `video` | ❌ | http/https URL or HA-relative path. Persistent notification: clickable filename link. Signal: forwarded as URL or attachment. Not supported by Mobile App. URLs without a file path segment are ignored with a warning. |
| `file` | ❌ | http/https URL or HA-relative path. Persistent notification: clickable filename link. Signal: forwarded as URL or attachment. Not supported by Mobile App. URLs without a file path segment are ignored with a warning. |
| `link` | ❌ | http/https URL. Mobile app: sets tap action (`data.url` / `data.clickAction`). Persistent notification: rendered as a `[Details]` link. Signal: appended to the message body. |
| `context` | ❌ | key-value dict. Persistent notification: appended to the message body as a `Context:` section; values matching a known HA entity ID are auto-linked to their history page. Mobile app: the `entity` key sets a tap-action deep-link (`entityId:<entity_id>`) when `link` is not set. Signal and TTS ignore all context keys. |
| `actions` | ❌ | list of up to 3 action button dicts (`action`, `title`, optional `uri`) — forwarded to Mobile App only; ignored by other channels |
| `channel_data` | ❌ | adapter-specific delivery overrides. Signal: supports `text_mode`, `attachments`, `urls`, `verify_ssl`. Mobile app: all keys are flat-merged into the `data:` payload (e.g. `{"tag": "my-tag"}` to control the notification tag for grouping or acknowledgement tracking). |

For real-world automation patterns see [Usage Examples](docs/usage-examples.md). For the full service reference and delivery pipeline see [How It Works](docs/how-it-works.md). For per-channel field handling, `channel_data` options, and acknowledgement mechanics see [Channel Reference](docs/channels.md).

## Troubleshooting

If something isn't working as expected, the built-in diagnostics panel (**Settings ➔ Devices & Services ➔ Advanced Notification System ➔ ⋮ ➔ Download Diagnostics**) is the first place to look. For common issues and FAQ see [Troubleshooting](docs/troubleshooting.md).

## Advanced Topics

For per-channel adapter reference (field handling, `channel_data`, acknowledgement) see [Channel Reference](docs/channels.md). For storage internals, crash recovery details, channel adapter lifecycle, and extension points see [Advanced Topics](docs/advanced.md).

## Development and Maintenance

I basically created this integration for my personal purpose. As it fulfils all my current needs I won't develop it further for now.\
However, as long as I am using this integration in my Home Assistant setup I will maintain it actively.

## Contributions

If you want to contribute to this integration, please read the [Contribution guidelines](CONTRIBUTING.md)

### Providing Translations for Other Languages

If you would like to use the integration in another language, you can help out by providing the necessary translations in [custom_components/ans/translations/](./custom_components/ans/translations/) and open a pull request with the changes.
