# Advanced Notification System (ANS) for Home Assistant

[![GitHub Release](https://img.shields.io/github/release/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/commits/main)
[![License](https://img.shields.io/github/license/txxa/hass-ans.svg?style=for-the-badge)](https://github.com/txxa/hass-ans/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)

_A sophisticated notification management system for Home Assistant with advanced routing, filtering, and delivery control._

This integration provides a centralized notification system that intelligently routes messages to the right recipients through the right channels at the right time, with support for filtering, rate limiting, retry logic, and Do Not Disturb schedules.

## Table of contents

- [Features](#features)
- [Concept](#concept)
- [Installation](#installation)
- [Configuration](#configuration)
- [Development and Maintenance](#development-and-maintenance)
- [Contributions](#contributions)

## Features

- **Centralized Notification Management**: Single service call to send notifications to multiple recipients through multiple channels
- **Intelligent Routing**: Automatic recipient resolution and channel selection based on configuration
- **Advanced Filtering**:
  - Type-based filtering (info, warning, alert, reminder, event, security)
  - Criticality-based filtering (low, medium, high, critical)
  - Source-based blocking with regex pattern matching
  - Do Not Disturb schedules with bypass rules for critical notifications
- **Rate Limiting**:
  - Per-recipient rate limiting to control notification volume
  - Global system-wide rate limiting to prevent infrastructure overload
  - Token bucket algorithm with configurable capacity and refill rates
- **Robust Delivery**:
  - Automatic retry with exponential backoff for transient failures
  - Configurable retry attempts and delays
  - Idempotent delivery to prevent duplicates
  - Crash recovery with persistent state
- **Multi-Channel Support**:
  - System channels (persistent notification)
  - Recipient channels (mobile app, Signal Messenger, and other Home Assistant notification services)
  - Text-to-Speech (TTS) delivery via media players
  - Extensible adapter architecture for additional channels
- **Text-to-Speech Delivery**:
  - Time-based volume control with four configurable time frames (morning, daytime, evening, night)
  - Criticality-based volume overrides for urgent notifications
  - Automatic volume restoration after playback, backed by persistent storage so restores survive restarts
  - Configurable trailing silence padding to compensate for audio pipeline buffering (e.g., Snapcast)
  - Per-device delivery lock prevents overlapping TTS playback on the same media player
  - Deduplication to prevent the same message from playing multiple times
- **Comprehensive Tracking**:
  - Notification registry for all messages
  - Delivery attempt logs for audit trails
  - Persistent retry queue across restarts
  - Configurable retention periods for historical data (default: 7 days, max: 365 days)
  - Hourly housekeeping to automatically purge expired records
- **Diagnostics**: Built-in Home Assistant diagnostics panel shows channel health, adapter status, and recipient summary
- **Flexible Configuration**:
  - UI-based configuration flow
  - Per-recipient customization
  - System-wide defaults
  - Runtime reconfiguration without restart

## Concept

The Advanced Notification System (ANS) acts as a central notification hub for Home Assistant. When a notification is sent to the system, it orchestrates the delivery process through several intelligent stages to ensure messages reach their intended recipients effectively.

### How It Works

When a notification enters the system, the following process occurs:

1. **Notification Reception**: A notification is created with metadata including source, title, message, type (e.g., info, warning, alert), and criticality level (low, medium, high, critical).

2. **Configuration Snapshot**: The system captures the current configuration state to ensure consistent processing even if settings change during delivery.

3. **Recipient Resolution**: The system identifies all configured recipients who should potentially receive this notification based on their subscription settings.

4. **Channel Resolution**: For each recipient, the system determines which delivery channels (such as mobile app, persistent notification, or other notification services) should be used based on the recipient's channel configuration.

5. **Task Fan-out**: Individual delivery tasks are created for each combination of recipient and channel, ensuring isolated processing and delivery tracking.

6. **Filtering**: Each delivery task is evaluated against the recipient's notification policy:
   - **Type filtering**: Only notification types the recipient has subscribed to are allowed through
   - **Source blocking**: Notifications from blocked sources are filtered out
   - **Do Not Disturb (DND)**: Time-based DND schedules can block notifications during specific hours, with configurable bypass rules for critical messages or important sources

7. **Rate Limiting**: The system enforces rate limits to prevent notification flooding:
   - **Per-recipient limits**: Each recipient has their own rate limit to control notification volume
   - **Global limits**: A system-wide rate limit prevents overwhelming the entire notification infrastructure
   - Rate-limited notifications are automatically queued for retry

8. **Delivery Execution**: Approved notifications are delivered through their designated channels using specialized delivery adapters that interface with Home Assistant's notification services.

9. **Retry Management**: Failed deliveries are handled intelligently:
   - **Transient failures** (temporary network issues, service unavailable): Automatically retried with exponential backoff
   - **Permanent failures** (invalid configuration, missing recipient): Logged and not retried
   - Each retry attempt is tracked with configurable maximum retry counts

10. **Persistence and Audit**: The system maintains comprehensive records:
    - Notification registry tracks all notifications
    - Delivery attempt logs provide audit trails
    - Retry queue manages pending retries
    - All data persists across Home Assistant restarts

### Key Components

- **Orchestrator**: Coordinates the notification workflow and creates delivery tasks
- **Filter Engine**: Evaluates policies to determine if a notification should be delivered
- **Rate Limiter**: Enforces token bucket-based rate limiting at recipient and system levels
- **Delivery Processor**: Executes delivery tasks and manages the delivery lifecycle
- **Delivery Adapters**: Channel-specific implementations that interface with notification services (persistent notification, mobile app, Signal Messenger, TTS media player)
- **Channel Manager**: Unified source of truth for channel metadata and live adapter instances; auto-detects newly registered notify services and media players
- **Deduplication Service**: LRU-based cache with TTL expiration (60 s window) and automatic cleanup prevents the same notification from being delivered twice to the same channel
- **Retry Scheduler**: Manages exponential backoff and retry timing
- **Persistence Layer**: Ensures durability through file-based storage with hourly housekeeping
- **Volume Restoration Registry**: Persist the pre-TTS volume of each media player and restore it after playback even across Home Assistant restarts

The system is designed with fault tolerance in mind. All processing is idempotent, meaning operations can be safely retried without duplication. The persistent storage ensures that even after a Home Assistant restart, pending notifications and retry queues are preserved and processing continues seamlessly.

## Installation

1. Add this repository as a custom repository to HACS: [![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=txxa&repository=hass-ans&category=integration)
2. Use HACS to install the integration.
3. Restart Home Assistant.
4. Set up the integration using the UI: [![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ans)

## Configuration

Configuration is done through the Home Assistant UI and consists of two parts: the main integration configuration and individual recipient configurations.

### Integration Configuration (Main Entry)

To add the integration, go to Settings ➤ Devices & Services ➤ Integrations, click ➕ Add Integration, and search for "Advanced Notification System" or "ANS".

#### Initial Setup

During initial setup, the following settings are configured:

- **Enabled Channels**: Select which notification channels are available system-wide, including media player entities for TTS delivery
- **TTS Service** (optional): Select a TTS integration to enable spoken audio notifications (e.g., `tts.google_translate_say`)
- **Audit Logging**: Enable or disable comprehensive delivery tracking and audit logs

#### Integration Options

After installation, additional system-wide settings can be configured via the Options menu (Settings ➤ Devices & Services ➤ Integrations ➤ Advanced Notification System ➤ Configure):

- **Global Rate Limit**: Maximum number of notifications per minute across the entire system
- **Retry Base Delay**: Initial delay in seconds before the first retry attempt
- **Retry Backoff Multiplier**: Factor for exponential backoff between retry attempts
- **Retry Max Delay**: Maximum delay in seconds between retry attempts
- **Queue Max Concurrency**: Number of parallel delivery tasks that can run simultaneously
- **Audit Log Retention**: Days to retain audit log data (only available if audit logging is enabled)

#### Reconfiguring Integration Settings

The initial setup options (TTS Service, Enabled Channels, and Audit Logging) can be changed at any time using the Reconfigure flow (Settings ➤ Devices & Services ➤ Integrations ➤ Advanced Notification System ➤ Reconfigure).

### Recipient Configuration (Sub Entries)

Individual recipients must be configured to receive notifications. Each recipient is added as a separate configuration entry.

#### Adding a Recipient

To add a new recipient:

1. Go to Settings ➤ Devices & Services ➤ Integrations ➤ Advanced Notification System
2. Click "Add Recipient"
3. Follow the configuration steps:

**Step 1: Select/Create Recipient**

Choose the recipient type:
- **System: Home Assistant**: A special built-in recipient for system-level notifications via persistent notification. Recipient data (Step 2) is skipped — name and settings are applied automatically and cannot be changed.
- **Home Assistant users** (listed individually): Links the recipient to an existing HA user account. Only users not yet configured as recipients appear in this list.
- **TTS: Text-to-Speech recipient**: Creates a recipient that delivers spoken notifications to media player entities (requires a TTS service to be configured in the initial setup)
- **Virtual: Custom recipient**: Creates a custom recipient identified by display name, email address, or phone number

**Step 2: Recipient Data** *(skipped for System: Home Assistant)*
- **Display Name**: Friendly name for the recipient
- **Email Address**: Email contact for the recipient (if applicable, not applicable for TTS recipients)
- **Phone Number**: Phone contact for the recipient (if applicable, not applicable for TTS recipients)

> **Note**: Contact info requirements depend on channels:
> - Mobile app channels require Home Assistant user linkage (no email/phone needed)
> - Email channels require email address
> - SMS/Messenger channels require phone number
> - System channels (persistent notification) and TTS recipients require no contact info

**Step 3: Basic Settings**
- **Rate Limit**: Maximum notifications per minute for this recipient
- **Maximum Retry Attempts**: Number of retry attempts for failed deliveries
- **Allowed Notification Types**: Select which notification types (INFO, WARNING, ALERT, REMINDER, EVENT, SECURITY) this recipient should receive
- **Blocked Sources**: Regular expression patterns to block notifications from specific sources

**Step 4: TTS Settings** *(TTS recipients only)*
- **Volume Levels**: Configure the playback volume for each time frame:
  - Morning (06:00–09:00): default 40%
  - Daytime (09:00–18:00): default 50%
  - Evening (18:00–22:00): default 40%
  - Night (22:00–06:00): default 20%
- **Override Volume Level**: Volume used when the criticality override is triggered (default 80%)
- **Override for Criticalities**: Select which criticality levels trigger the override volume (e.g., HIGH, CRITICAL)
- **Message Format**: How notification content is spoken:
  - `title_and_message`: speaks "{title}. {message}" *(default)*
  - `message_only`: speaks only the message
  - `title_only`: speaks only the title
- **Trailing Silence Padding** (ms): Extra silence appended after the spoken message (default 1000 ms). Compensates for audio pipeline buffering, such as Snapcast's default buffer size. Increase this value if the end of the message is cut off. Maximum: 5000 ms.

**Step 5: Channel Mapping**
- Map each criticality level (LOW, MEDIUM, HIGH, CRITICAL) to specific delivery channels
- For the System: Home Assistant recipient: `persistent_notification` (the only available channel for this recipient type)
- For notification recipients: notification services (e.g., `notify.mobile_app_phone`)
- For TTS recipients: media player entities (e.g., `media_player.living_room_speaker`)

**Step 6: Do Not Disturb Settings**
- **Enable/Disable DND**: Turn Do Not Disturb mode on or off
- **Start Time**: Time when DND period begins
- **End Time**: Time when DND period ends
- **Allowed Sources During DND**: Sources that can bypass DND (optional)
- **Allowed Criticality Levels During DND**: Criticality levels that bypass DND (optional)
- **Allowed Notification Types During DND**: Notification types that bypass DND (optional)

#### Reconfiguring Recipients

All recipient settings can be updated at any time using the Reconfigure flow for the specific recipient entry.

### Services

ANS registers two services.

#### `ans.send_notification`

Send a notification through the delivery pipeline:

```yaml
service: ans.send_notification
data:
  source: "security_system"
  title: "Motion Detected"
  message: "Motion detected at front door"
  type: "ALERT"
  criticality: "HIGH"
  metadata:
    camera: "front_door"
    entity_id: "binary_sensor.front_door_motion"
```

**Parameters**:
| Field | Required | Values | Description |
|---|---|---|---|
| `source` | ✅ | any string | Identifier of the notification source |
| `title` | ✅ | any string | Notification title |
| `message` | ✅ | any string | Notification message body |
| `type` | ✅ | `INFO` `WARNING` `ALERT` `REMINDER` `EVENT` `SECURITY` | Notification category |
| `criticality` | ✅ | `LOW` `MEDIUM` `HIGH` `CRITICAL` | Priority level used for channel mapping, rate limiting, and DND bypass |
| `metadata` | ❌ | key-value dict | Optional additional data attached to the notification |

#### `ans.refresh_channels`

Reload all notification channels and re-synchronize delivery adapters. Use this service after adding a new notify integration or media player entity, or when channels are not appearing in ANS:

```yaml
service: ans.refresh_channels
```

This service takes no parameters. It triggers a full re-detection of available `notify.*` services and `media_player.*` entities and rebuilds the active adapter set accordingly.

## Development and maintenance

I basically created this integration for my personal purpose. As it fulfils all my current needs I won't develop it further for now.\
However, as long as I am using this integration in my Home Assistant setup I will maintain it actively.

## Contributions

If you want to contribute to this integration, please read the [Contribution guidelines](CONTRIBUTING.md)

### Providing translations for other languages

If you would like to use the integration in another language, you can help out by providing the necessary translations in [custom_components/ans/translations/](./custom_components/ans/translations/) and open a pull request with the changes.
