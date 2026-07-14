# Advanced Notification System (ANS) for Home Assistant

[![GitHub Release](https://img.shields.io/github/release/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/commits/main)
[![License](https://img.shields.io/github/license/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)

_A sophisticated notification management system for Home Assistant with advanced routing, filtering, and delivery control._

ANS is a custom Home Assistant integration that acts as a centralized notification hub. Instead of calling `notify.mobile_app_*`, `notify.signal`, or TTS services individually from each automation, you make a single `ans.send_notification` call and ANS handles the rest: routing to the right recipients, choosing the right channels based on criticality, applying Do Not Disturb schedules, rate limiting, retrying on failure, and managing TTS volume. There is no `target` parameter to set — who gets notified, and through which channel, is entirely decided by recipient configuration, not by the service call.

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

- **Single service call** fans out to every configured recipient and channel
- **Criticality-based routing** — LOW through CRITICAL, each mapped to a different set of channels per recipient
- **Per-recipient filtering** — notification type allow-lists, source blocking, and Do Not Disturb schedules with bypass rules
- **Flood protection** — token-bucket rate limiting (per-recipient and global) plus a bounded delivery queue, both with automatic retry queuing
- **Reliable delivery** — automatic retries with exponential backoff, crash recovery across HA restarts, and deduplication (including TTS)
- **Multi-channel delivery out of the box** — Mobile App, Signal Messenger, and Persistent Notification, with an extensible adapter architecture for more
- **TTS via media player** — time-of-day and criticality-based volume control with automatic restoration
- **Actionable mobile notifications** — up to 3 tap-to-respond action buttons
- **Acknowledgement tracking** — know when a notification was actually seen (tap or dismissal), recorded in a restart-safe registry
- **Audit log** — every notification and delivery attempt recorded, with configurable retention
- **Real-time delivery events** — automations can react to delivery, filtering, failure, rate-limiting, settlement, and acknowledgement as they happen
- **Self-healing channel detection** — a missing channel raises a Repairs issue and resolves itself when the channel comes back
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

> **No `target` field:** ANS has no `target` parameter and does not support one — you cannot address a specific device, entity, or person in the service call. Every recipient's channel mapping, filters, and Do Not Disturb schedule (configured once in the integration) decide who receives a notification and how. To change who is notified, edit the recipient configuration — never the automation.

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

Development of this integration was assisted by AI tools.

## Contributions

If you want to contribute to this integration, please read the [Contribution guidelines](CONTRIBUTING.md)

### Providing Translations for Other Languages

If you would like to use the integration in another language, you can help out by providing the necessary translations in [custom_components/ans/translations/](./custom_components/ans/translations/) and open a pull request with the changes.
