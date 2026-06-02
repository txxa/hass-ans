# Overview

*Next: [Installation & Configuration →](getting-started.md)*

An introduction to ANS: what it is, what problems it solves, and what you need to get started.

## Contents

- [What is ANS?](#what-is-ans)
- [Key Features](#key-features)
  - [Multi-Channel Delivery](#multi-channel-delivery)
  - [Actionable Mobile Notifications](#actionable-mobile-notifications)
  - [Acknowledgement Tracking](#acknowledgement-tracking)
  - [Criticality-Based Routing](#criticality-based-routing)
  - [Intelligent Filtering](#intelligent-filtering)
  - [Rate Limiting](#rate-limiting)
  - [Reliable Delivery](#reliable-delivery)
  - [TTS Volume Management](#tts-volume-management)
  - [Audit Logging](#audit-logging)
  - [Stale Channel Repairs](#stale-channel-repairs)
  - [Built-in Diagnostics](#built-in-diagnostics)
  - [Flexible Configuration](#flexible-configuration)
- [Use Cases](#use-cases)
- [Requirements](#requirements)
  - [Home Assistant](#home-assistant)
  - [Installation Method](#installation-method)
  - [Per-Channel Prerequisites](#per-channel-prerequisites)

## What is ANS?

The **Advanced Notification System (ANS)** is a custom Home Assistant integration that acts as a centralized notification hub. Instead of calling `notify.mobile_app_*`, `notify.signal`, or TTS services individually from each automation, you make a single `ans.send_notification` call and ANS handles the rest: choosing which recipients get the notification, which channels to use, when to deliver it, and what to do if delivery fails.

ANS decouples *what to send* from *how and to whom to send it*. Routing decisions — channels per criticality level, Do Not Disturb schedules, rate limits, retry behavior — live in recipient configuration, not in your automations.

---

## Key Features

### Multi-Channel Delivery
Route a single notification to any combination of:
- **Persistent Notification** — HA frontend sidebar (always available); `image`, `video`, and `file` links render with the filename as the label; `context` key-value pairs appear as a `Context:` section with entity IDs auto-linked
- **Mobile App** — Push notifications via the HA Companion App, with optional action buttons
- **Signal Messenger** — Messages via the Signal integration, with support for styled text, file attachments (restricted to HA-managed directories), and image URLs; top-level `image`, `video`, and `file` fields are automatically routed to Signal; media URLs without a filename path segment are silently ignored with a warning log
- **TTS via Media Player** — Spoken announcements through any media player entity that supports playback and volume control
- **Extensible adapter architecture** — additional channels can be added without modifying core delivery logic

### Actionable Mobile Notifications
Mobile push notifications can include up to 3 action buttons defined in the top-level `actions` field of `ans.send_notification`. When a button is tapped, the HA Companion App fires a `mobile_app_notification_action` event on the HA bus. Automations can listen for that event and react — closing a garage door, unlocking a door, or acknowledging an alert. Action buttons are ignored by all other channels (Signal, TTS, persistent notification).

### Acknowledgement Tracking
ANS tracks whether a delivered notification was acknowledged by the recipient. Two acknowledgement sources are supported:

- **Mobile App**: tapping any action button on a push notification counts as an acknowledgement. ANS correlates the tap via `data.tag` (set to the `notification_id` UUID automatically) and records it in the `AcknowledgementRegistry`.
- **Persistent Notification**: dismissing a persistent notification in the HA sidebar counts as an acknowledgement. ANS listens for the dismissal signal from the HA dispatcher.

When either source fires, ANS records the acknowledgement and fires an `ans_notification_acknowledged` HA bus event. Automations can correlate the `notification_id` (returned by `ans.send_notification` via `response_variable`) with this event to implement confirmed read receipts, automatic escalation cancellation, or acknowledgement dashboards.

Acknowledgement state is stored in `ans_acknowledgements.json` in HA `.storage/` and survives restarts. Each notification can only be acknowledged once — the registry is idempotent.

> **Note:** Acknowledgement tracking requires the notification to have been delivered in the **current HA session**. Notifications delivered before the last HA restart cannot be acknowledged — the pending-acks set is not persisted across restarts.

### Criticality-Based Routing
Every notification carries a criticality level (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`). Each recipient maps each level to a different set of channels. A LOW-criticality reminder might go only to the HA sidebar; a CRITICAL security alert goes to mobile, Signal, and every speaker in the house.

### Intelligent Filtering
Each recipient has configurable filters applied before delivery:
- **Type allow-list** — only receive `INFO`, `ALERT`, `REMINDER`, etc. that the recipient opted into
- **Source blocking** — regex pattern to suppress notifications from specific automations or scripts
- **Do Not Disturb** — quiet hours window with fine-grained bypass rules (by source, criticality, or notification type)

### Rate Limiting
Two layers of protection against notification floods:
- **Per-recipient** — cap how many notifications a single recipient receives per minute
- **Global** — system-wide ceiling across all recipients combined

Both use a token bucket algorithm with configurable capacity and window.

### Reliable Delivery
- **Automatic retries** with exponential backoff for transient failures (network issues, service unavailable); retry attempts and delay intervals are configurable
- **Deduplication** — prevents the same notification from being delivered to the same channel twice within a 60-second window, including duplicate TTS playback
- **Crash recovery** — pending retries are persisted to disk and replayed automatically after a Home Assistant restart

### TTS Volume Management
For TTS recipients, ANS automatically adjusts media player volume based on time of day (morning, daytime, evening, night) and criticality level. After playback, the original volume is restored. Volume state is persisted so restoration works even across restarts. A per-device delivery lock prevents overlapping TTS playback on the same media player.

### Audit Logging
When enabled, ANS records every notification and every delivery attempt to JSON files in the HA `.storage/` directory. The retention period is configurable (default: 7 days, max: 365 days). Hourly housekeeping automatically purges expired records.

### Stale Channel Repairs
When a `notify.*` service or `media_player.*` entity disappears from Home Assistant (companion app uninstalled, integration removed, entity renamed), ANS marks the channel as `STALE` and raises a HA **Repairs** issue in **Settings → System → Repairs**. The issue names the missing channel and explains exactly how to resolve it — either by running `ans.refresh_channels` to re-detect the channel, or by reconfiguring the affected recipients to use a different channel. The Repairs issue is automatically dismissed once the channel is detected as active again.

### Built-in Diagnostics
The standard HA Diagnostics panel (Settings → Integrations → ANS → Download Diagnostics) provides a snapshot of channel health, adapter status, and recipient counts — without exposing any personal data.

### Flexible Configuration
- **UI-based configuration flow** — the entire setup is done through the HA integrations UI, no YAML required
- **Per-recipient customization** — channels, filters, DND schedules, and rate limits are configured independently per recipient
- **System-wide defaults** — global settings for audit logging, TTS service, and rate limits apply across all recipients
- **Runtime reconfiguration** — channels and options can be changed at any time without restarting Home Assistant

---

## Use Cases

**Multi-person household**
Each family member is a separate ANS recipient with their own device, phone number, and DND schedule. A single service call fans out appropriately to everyone.

**Criticality escalation**
A garage door left open sends a LOW push notification after 5 minutes, then escalates to HIGH (mobile + TTS) after 15 minutes, and CRITICAL (all channels) if you're away from home.

**Overnight quiet hours with smart exceptions**
DND suppresses notifications from 22:00 to 07:00, but smoke alarm (`SECURITY` type) and CRITICAL-level alerts always break through.

**Centralized automation authoring**
All automations use a single service with consistent parameters. Changing *how* a recipient is notified (e.g., switching from Signal to a new channel) requires only updating the recipient config, not editing every automation.

**Accessible home**
TTS announcements through living room and bedroom speakers ensure notifications reach anyone regardless of phone availability.

---

## Requirements

### Home Assistant
- Version **2025.12.5** or newer

### Installation Method
- **[HACS](https://hacs.xyz/)** (recommended) — requires HACS to be installed
- **Manual** — no additional tools required

### Per-Channel Prerequisites

| Channel | Prerequisite |
|---|---|
| Persistent Notification | None — always available. **Exclusive to the HA System recipient type** (`Home Assistant (System)`); cannot be assigned to HA User, Generic, or TTS recipients. |
| Mobile App | [HA Companion App](https://companion.home-assistant.io/) installed and logged in on the device |
| Signal Messenger | [Signal integration](https://www.home-assistant.io/integrations/signal_messenger/) configured in HA with a running Signal API server |
| TTS via Media Player | A TTS integration (e.g., [Google Translate TTS](https://www.home-assistant.io/integrations/google_translate/), [Piper](https://www.home-assistant.io/integrations/wyoming/)) and at least one media player entity that supports both `PLAY_MEDIA` and `VOLUME_SET` |

> **Note:** You do not need all channels configured to use ANS. Install only the channels you plan to use and enable them during setup.

---

*Next: [Installation & Configuration →](getting-started.md)*
