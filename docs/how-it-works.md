# How It Works

*Previous: [← Installation & Configuration](getting-started.md) | Next: [Usage Examples →](usage-examples.md)*

The `ans.send_notification` service reference and a stage-by-stage walkthrough of the delivery pipeline.

## Contents

- [The Service Call](#the-service-call)
  - [Parameters](#parameters)
  - [Notification Types](#notification-types)
  - [Criticality Levels](#criticality-levels)
- [The Delivery Pipeline](#the-delivery-pipeline)
  - [Stage 1 — Configuration Snapshot](#stage-1--configuration-snapshot)
  - [Stage 2 — Recipient Resolution](#stage-2--recipient-resolution)
  - [Stage 3 — Channel Resolution](#stage-3--channel-resolution)
  - [Stage 4 — Task Fan-out](#stage-4--task-fan-out)
  - [Stage 5 — Filtering](#stage-5--filtering)
  - [Stage 6 — Rate Limiting](#stage-6--rate-limiting)
  - [Stage 7 — Delivery](#stage-7--delivery)
  - [Stage 8 — Retry on Failure](#stage-8--retry-on-failure)
  - [Stage 9 — Persistence](#stage-9--persistence)
- [Deduplication](#deduplication)
- [Delivery Outcome Events](#delivery-outcome-events)
- [Channel Detection and the Refresh Service](#channel-detection-and-the-refresh-service)
  - [Stale Channel Repairs Issues](#stale-channel-repairs-issues)

## The Service Call

Everything in ANS begins with a single service call: `ans.send_notification`. This is the one entry point for all notifications in your Home Assistant setup, regardless of who receives them or how they are delivered.

```yaml
service: ans.send_notification
data:
  source: "automation.front_door"
  title: "Front Door Opened"
  message: "The front door was opened at 3:45 PM."
  type: SECURITY
  criticality: HIGH
```

### Parameters

| Parameter | Required | Type | Description |
|---|---|---|---|
| `source` | Yes | string | Free-form identifier for the sender. Used for source-blocking filters. Convention: use the automation or script entity ID (e.g. `automation.motion_alert`) or a logical name (e.g. `security_system`). |
| `title` | Yes | string | Subject line of the notification. |
| `message` | Yes | string | Body of the notification. |
| `type` | Yes | select | Notification category. Controls type-based filtering per recipient. |
| `criticality` | Yes | select | Priority level. Determines which channels are used per recipient. |
| `metadata` | No | dict | Optional key-value data. Passed to channel adapters for channel-specific features (e.g. Signal attachments, mobile app action buttons). |

### Notification Types

| Value | Intended Use |
|---|---|
| `INFO` | General information, status updates |
| `WARNING` | Something needs attention but is not urgent |
| `ALERT` | Requires prompt action |
| `REMINDER` | Scheduled or recurring reminders |
| `EVENT` | Something happened (doorbell, package delivery) |
| `SECURITY` | Security-related events |

### Criticality Levels

| Value | Intended Use |
|---|---|
| `LOW` | Informational, low urgency |
| `MEDIUM` | Standard priority |
| `HIGH` | Elevated priority, prompt attention needed |
| `CRITICAL` | Immediate attention required, bypasses most filters |

> **The key insight:** `type` controls *filtering* (which recipients want this kind of notification), while `criticality` controls *routing* (which channels each recipient uses to receive it). A `SECURITY` alert at `LOW` criticality goes to the sidebar only; the same alert at `CRITICAL` goes to every configured channel.

---

## The Delivery Pipeline

After `ans.send_notification` is called, ANS processes the notification through nine stages. Understanding this pipeline helps you configure recipients correctly and debug unexpected behavior.

```
ans.send_notification
        │
        ▼
1. Configuration Snapshot
        │
        ▼
2. Recipient Resolution
        │
        ▼
3. Channel Resolution (per recipient × criticality)
        │
        ▼
4. Task Fan-out
        │
  ┌─────┴──────┐
  ▼            ▼
Task 1      Task 2  ...  (one task per recipient + channel pair)
  │
  ▼
5. Filtering
  │ (if allowed)
  ▼
6. Rate Limiting
  │ (if allowed)
  ▼
7. Delivery
  │
  ├─ SUCCESS → persist, done
  ├─ TRANSIENT_FAIL → schedule retry
  └─ PERMANENT_FAIL → persist, no retry
```

### Stage 1 — Configuration Snapshot

ANS captures an immutable snapshot of the current configuration (all recipients, their policies, and system settings) before processing begins. This ensures every delivery task for a given notification uses consistent settings, even if you change configuration mid-delivery. Snapshots are also persisted to disk so in-flight tasks can be replayed after a restart.

### Stage 2 — Recipient Resolution

ANS identifies all configured recipients. In the current version, every active recipient is considered for each notification. Per-recipient filtering in Stage 5 handles whether a given recipient actually receives it.

### Stage 3 — Channel Resolution

For each recipient, ANS looks up which channels are mapped to the notification's criticality level. This comes from the recipient's channel mapping configuration (Step 5 of the recipient wizard).

For example, if Alice has:
- LOW → `notify.persistent_notification`
- HIGH → `notify.mobile_app_alice`, `notify.signal`

…and the notification has `criticality: HIGH`, then Alice gets tasks for `notify.mobile_app_alice` and `notify.signal`.

If a criticality level has no channels configured for a recipient, that recipient receives nothing for that level (silent suppression — not an error).

### Stage 4 — Task Fan-out

ANS creates one independent delivery task for each `(recipient, channel)` pair. Tasks are enqueued in the delivery queue and processed concurrently up to the configured `queue_max_concurrency` limit. Each task is self-contained: it carries a full copy of the payload, recipient policy, contact info, and the configuration snapshot reference.

### Stage 5 — Filtering

Each task is evaluated against three filters in sequence. Any filter that blocks the notification terminates the task permanently — no retry is scheduled.

**Type filter:**
If the recipient has a notification type allow-list configured, only notifications whose `type` appears in that list pass through. All others are silently discarded.

```
notification.type ∈ recipient.allowed_types  →  pass
notification.type ∉ recipient.allowed_types  →  FILTERED
```

**Source block:**
If the recipient has a `blocked_sources_regex` pattern, notifications whose `source` matches the pattern are discarded.

```
re.match(blocked_sources_regex, notification.source) → FILTERED
```

**Do Not Disturb (DND):**
If the current time falls within the recipient's DND window, the notification is filtered — unless it matches a bypass rule. Bypass rules are checked in order:

1. `dnd_allowed_sources_regex` — source matches pattern → **allowed** (DND bypassed)
2. `dnd_allowed_criticalities` — notification criticality in bypass list → **allowed**
3. `dnd_allowed_types` — notification type in bypass list → **allowed**
4. None matched → **FILTERED** (DND active)

DND windows support midnight-crossing (e.g. 22:00–06:00 works correctly).

### Stage 6 — Rate Limiting

Rate limiting uses a token bucket algorithm. Two buckets are checked:

1. **Global bucket** — shared across all recipients system-wide. Controlled by `global_rate_limit` in Options.
2. **Per-recipient bucket** — individual to each recipient. Controlled by each recipient's `rate_limit` setting.

Both buckets must have available tokens for delivery to proceed. If either bucket is empty, the task is logged as `RATE_LIMITED` and a retry is scheduled using the same exponential backoff formula as transient failures.

Setting a rate limit to `0` disables that layer of limiting.

### Stage 7 — Delivery

ANS selects the live adapter for the target channel and calls `deliver()`. Each channel adapter handles the specifics:

**Persistent Notification**
Calls `persistent_notification.create` in HA. The notification appears in the sidebar immediately. Metadata key-value pairs are appended to the message body.

**Mobile App**
Calls `notify.mobile_app_{device_id}`. The `title` and `message` are sent as-is. The full `metadata` dict is passed through as the `data:` payload, so all standard [HA Companion App notification features](https://companion.home-assistant.io/docs/notifications/notifications-basic/) (action buttons, tags, channels, images) work via `metadata`.

**Signal Messenger**
Calls `notify.signal` with the recipient's phone number. When `text_mode: styled` is set in metadata (or when a title is present without an explicit mode), the message is formatted with bold title (`**Title**`). Supports file attachments (`attachments:` list of local paths) and image URLs (`urls:` list) via metadata.

**TTS via Media Player**
Calls the configured TTS service targeting the media player entity. If volume management is enabled, ANS:
1. Reads the current media player volume
2. Sets volume to the time-of-day level (or criticality override level)
3. Speaks the message
4. Restores the original volume when playback ends (IDLE state detected)

A per-device delivery lock serializes concurrent TTS requests to the same media player.

### Stage 8 — Retry on Failure

**Retryable failures (TRANSIENT_FAIL):** Network errors, service temporarily unavailable, TTS engine busy. ANS schedules a retry with exponential backoff:

```
delay = min(retry_base_delay × backoff_factor^(attempt - 1), retry_max_delay)

# With defaults (base=60s, factor=2, max=3600s):
# Attempt 1 fails → retry in 60s
# Attempt 2 fails → retry in 120s
# Attempt 3 fails → retry in 240s
# After max_attempts → permanent failure, no more retries
```

Retries older than 2 hours are automatically discarded to prevent stale notifications from arriving long after the relevant moment has passed.

**Non-retryable failures (PERMANENT_FAIL):** Missing contact info (phone number required but not configured), adapter permanently rejected the request.

**Filtered tasks** are not retried — filtering is a policy decision, not a delivery error.

### Stage 9 — Persistence

When audit logging is enabled (default), ANS writes two records per notification:

- **`ans_notifications.json`** — one entry per `send_notification` call: source, title, type, criticality, recipient list, timestamp
- **`ans_delivery_attempts.json`** — one entry per delivery attempt: channel, recipient, status, attempt number, error (if any), timestamps

These files are stored in `<config>/.storage/` and cleaned up automatically based on the configured retention period. See [Advanced Topics](advanced.md) for details.

---

## Deduplication

ANS maintains an LRU cache (max 1 000 entries, 60-second TTL) keyed on `(notification_id, channel_id)`. If the same notification is delivered to the same channel twice within 60 seconds — for example due to a race condition or misconfigured automation — the second delivery is silently dropped. After 60 seconds, delivery to the same `(notification, channel)` pair is permitted again.

---

## Delivery Outcome Events

ANS fires Home Assistant bus events at every terminal outcome of the delivery pipeline. Automations and scripts can subscribe to these events to build escalation logic, dashboards, or monitoring.

### Per-Channel Events

One event is fired per channel task when it reaches a terminal state:

| Event | Fired when |
|---|---|
| `ans_notification_delivered` | Channel adapter confirmed delivery |
| `ans_notification_filtered` | Task dropped by type/criticality/DND/source filter |
| `ans_notification_failed` | All retry attempts exhausted (permanent failure) |
| `ans_notification_rate_limited` | Task rate-limited; queued for retry or dropped |

All four events share a base payload:

| Field | Type | Description |
|---|---|---|
| `notification_id` | `str` | UUID v4 identifying the notification |
| `recipient_id` | `str` | Configured recipient ID |
| `channel_id` | `str` | Channel that attempted delivery |
| `source` | `str` | Integration or automation that called `ans.send_notification` |
| `criticality` | `str` | Criticality level (e.g. `LOW`, `CRITICAL`) |
| `type` | `str` | Notification type (e.g. `INFO`, `ALERT`) |

Extra fields per event:

| Event | Extra fields |
|---|---|
| `ans_notification_delivered` | `attempt_number: int`, `remote_id: str \| null` |
| `ans_notification_filtered` | `filter_reason: str` (the `FilterReason` enum value) |
| `ans_notification_failed` | `error: str \| null`, `attempt_number: int` |
| `ans_notification_rate_limited` | `limit_type: str` (`"GLOBAL"` or `"RECIPIENT"`), `retry_at: str \| null` (ISO-8601; `null` when retries are exhausted) |

### The Settled Event

`ans_notification_settled` fires once **all** fan-out tasks for a notification reach a terminal state. It provides an aggregate view across all recipients and channels:

| Field | Type | Description |
|---|---|---|
| `notification_id` | `str` | UUID v4 identifying the notification |
| `total_tasks` | `int` | Total channel tasks dispatched |
| `total_recipients` | `int` | Number of unique recipients addressed |
| `delivered` | `int` | Tasks that reached `SUCCESS` |
| `failed` | `int` | Tasks that reached `PERMANENT_FAIL` |
| `filtered` | `int` | Tasks dropped by a filter |
| `recipients_delivered` | `int` | Recipients with at least one channel delivered |
| `recipients` | `dict` | Per-recipient channel breakdown (see [Payload Reference](advanced.md#delivery-outcome-events--payload-reference)) |

The `recipients_delivered` field is the key field for "did anyone receive this?" escalation logic: if it is `0`, no channel delivered successfully for any recipient.

### Service Response and Event Correlation

`ans.send_notification` returns a response payload:

```yaml
{ "notification_id": "<uuid>" }
```

Capture it with `response_variable` and use the ID to correlate the service call with delivery outcome events:

```yaml
action: ans.send_notification
data:
  message: "Security alert"
  type: SECURITY
response_variable: ans_result
# ans_result.notification_id can now be matched against event data.notification_id
```

Events fire after persistence, so the audit log already contains the matching record by the time your automation reacts. See [Usage Examples](usage-examples.md#reacting-to-delivery-outcomes) for full automation patterns and [Payload Reference](advanced.md#delivery-outcome-events--payload-reference) for exact field definitions.

---

## Channel Detection and the Refresh Service

ANS automatically detects available channels at startup and responds to live HA events:
- A new `notify.*` service is registered → channel list updated
- A `media_player.*` entity is added with the required features → added to TTS channel pool
- A `media_player.*` entity is removed → removed and adapter destroyed

For the rare case where auto-detection misses a change, the `ans.refresh_channels` service triggers a full manual re-scan:

```yaml
service: ans.refresh_channels
```

No parameters are required. See [Troubleshooting](troubleshooting.md#channel-not-appearing) for when to use this.

### Stale Channel Repairs Issues

When a channel that ANS previously knew about is no longer detected in HA — because the Companion App was uninstalled, an integration was removed, or an entity was renamed — the `ChannelManager` marks that channel as `STALE` and ANS raises a HA **Repairs issue** automatically. The issue appears in **Settings → System → Repairs** with:

- The name of the missing channel
- The channel ID (for use in reconfiguration)
- Instructions to run `ans.refresh_channels` or update the affected recipients

The Repairs issue is dismissed automatically as soon as the channel is detected as `ACTIVE` again — no manual action is needed if the channel recovers (e.g., phone reconnected, integration reinstalled). On HA startup, ANS also runs a cleanup sweep that dismisses any lingering Repairs issues for channels that are already back to `ACTIVE`.

Each stale channel produces exactly one Repairs issue. If the same channel goes stale and recovers multiple times, only one issue exists at any point in time.

See [Advanced Topics → Channel Manager — Adapter Lifecycle](advanced.md#channel-manager--adapter-lifecycle) for the full set of channel states.

---

*Previous: [← Installation & Configuration](getting-started.md) | Next: [Usage Examples →](usage-examples.md)*
