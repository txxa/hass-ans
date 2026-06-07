# How It Works

*Previous: [← Installation & Configuration](getting-started.md) | Next: [Channel Reference →](channels.md)*

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
- [Acknowledgement Tracking](#acknowledgement-tracking)
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
| `image` | No | string | http/https URL or HA-relative path (e.g. `/local/img.jpg`, `/api/camera_proxy/camera.front`). Persistent notification: http/https renders as a clickable `[filename](url)` link; local path renders as an inline Markdown image embed. Mobile app: forwarded as push image (http/https only). Signal: forwarded as a URL or attachment. URLs without a file path segment (e.g. bare domains) are silently ignored with a warning log. |
| `video` | No | string | http/https URL or HA-relative path. Persistent notification: rendered as a clickable `[filename](url)` link. Signal: forwarded as a URL or attachment. Not consumed by Mobile App or TTS. URLs without a file path segment are silently ignored with a warning log. |
| `file` | No | string | http/https URL or HA-relative path. Persistent notification: rendered as a clickable `[filename](url)` link. Signal: forwarded as a URL or attachment. Not consumed by Mobile App or TTS. URLs without a file path segment are silently ignored with a warning log. |
| `link` | No | string | http/https URL. Mobile app: sets `data.url` / `data.clickAction` (tap action). Persistent notification: rendered as a `[Details](url)` link. Signal: appended as plain text to the message body. |
| `context` | No | dict | Key-value pairs. Persistent notification: appended to the message body as a `Context:` section; values that match a known HA entity ID are auto-linked to the entity history page. Mobile app: the `entity` key is used as a tap-action deep-link (`entityId:<entity_id>`) when `link` is not set; all other keys are ignored. Signal and TTS ignore all context keys. |
| `actions` | No | list | Optional list of up to 3 action button objects. Each object requires `action` (identifier string) and `title` (button label), with an optional `uri`. Forwarded to Mobile App only; ignored by all other channels. |
| `channel_data` | No | dict | Adapter-specific delivery overrides (flat dict). Signal reads `text_mode`, `attachments`, `urls`, and `verify_ssl` from this dict. Mobile app flat-merges this dict into the `data:` payload; set `{"tag": "my-tag"}` to override the default acknowledgement-tracking tag (which defaults to the `notification_id` UUID). |

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

The delivery queue is **bounded** by the `queue_max_depth` setting (default: 500). If the queue is full when a new task arrives — for example during an automation storm — the task is dropped immediately, a warning is logged, and an `ans_notification_failed` event is fired with `error: "queue_full"`. Tasks in the persistent retry queue that cannot be re-enqueued when the queue is full are deferred to the next retry cycle rather than discarded.

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

ANS selects the live adapter for the target channel and calls `deliver()`. Each channel adapter handles field rendering, attachment routing, and acknowledgement setup differently. For the full per-channel reference — field handling, `channel_data` options, acknowledgement mechanics, and limitations — see [Channel Reference](channels.md).

**Persistent Notification** — calls `persistent_notification.create`. Rich-content fields (`image`, `video`, `file`, `link`, `context`) are rendered as Markdown in the sidebar. See [Persistent Notification → Field Handling](channels.md#field-handling-persistent-notification).

**Mobile App** — calls `notify.mobile_app_{device_id}`. `channel_data` is flat-merged into the `data:` payload, giving access to all Companion App features. `data.tag` is always set for acknowledgement tracking. See [Mobile App → Field Handling](channels.md#field-handling-mobile-app).

**Signal Messenger** — calls `notify.signal` with the recipient's phone number. Top-level `image`, `video`, and `file` fields are automatically routed to `urls` or `attachments` based on whether they are remote URLs or local paths. Attachment paths are validated against the HA-managed directory allowlist before forwarding. See [Signal Messenger → Field Handling](channels.md#field-handling-signal).

**TTS via Media Player** — calls the configured TTS service targeting the media player entity. If volume management is enabled, ANS captures the current volume, sets the time-of-day or criticality-override level, speaks the message, then restores the original volume when the player reaches IDLE. A per-device delivery lock prevents overlapping playback. See [TTS via Media Player](channels.md#tts-via-media-player).

**Standby devices (`off` state):** Platforms like Google Cast report `off` in standby but wake up natively when a play command is received. ANS delivers directly without attempting to set volume first. If the service call fails, the task is retried with exponential backoff.

**Unreachable devices (`unavailable` state):** ANS skips delivery and schedules a retry. This covers devices that are genuinely unreachable (network error, integration offline, entity removed).

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

ANS maintains an in-memory LRU cache keyed on `(notification_id, channel_id)`. If the same notification is delivered to the same channel twice within the TTL window — for example due to a race condition or misconfigured automation — the second delivery is silently dropped. For cache size, TTL, and restart-boundary behavior see [Advanced Topics → Deduplication — LRU Cache Details](advanced.md#deduplication--lru-cache-details).

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

`ans_notification_settled` fires once **all** fan-out tasks for a notification reach a terminal state. It provides an aggregate view across all recipients and channels. Key fields:

| Field | Description |
|---|---|
| `notification_id` | UUID v4 identifying the notification |
| `recipients_delivered` | Recipients with at least one channel delivered — the key field for "did anyone receive this?" escalation logic. If `0`, no channel delivered successfully for any recipient. |
| `total_tasks`, `delivered`, `failed`, `filtered` | Aggregate task counts |
| `recipients` | Per-recipient channel breakdown |

For the full JSON schema and complete field definitions see [Payload Reference](advanced.md#delivery-outcome-events--payload-reference).

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

## Acknowledgement Tracking

After a notification is delivered, ANS monitors for user acknowledgement and fires an `ans_notification_acknowledged` HA bus event when one is detected.

**End-to-end flow:**
1. `ans.send_notification` delivers the notification and returns a `notification_id` UUID.
2. For Mobile App: ANS sets `data.tag` to the `notification_id` on the push notification (or to `channel_data.tag` when provided — ANS records whichever tag was used at delivery time).
3. For Persistent Notification: ANS embeds the `notification_id` so the sidebar dismissal can be correlated.
4. The user taps an action button (Mobile App) or dismisses the notification from the sidebar (Persistent Notification).
5. ANS observes the event, matches it to a pending delivery, fires `ans_notification_acknowledged`, and records the acknowledgement in the `AcknowledgementRegistry`.

Automations can use the `notification_id` returned by `response_variable` to correlate service calls with this event. See [Usage Examples → Acknowledgement Tracking](usage-examples.md#acknowledgement-tracking) for full automation patterns.

### Trigger Sources

**Mobile App — button tap**
When a push notification is delivered via `MobileAppDeliveryAdapter`, ANS always sets `data.tag` on the notification to the `notification_id` UUID (the `channel_data.tag` field can override this — ANS records the active tag at delivery time so correlation is maintained regardless of which tag value is used). The HA Companion App includes the tag in every `mobile_app_notification_action` event it fires, regardless of which button was tapped. ANS listens for this event and checks whether the tag matches a pending delivery — if it does, the notification is acknowledged.

**Persistent Notification — sidebar dismissal**
When a persistent notification is dismissed from the HA sidebar, the HA dispatcher sends `SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED` with `UpdateType.REMOVED`. ANS connects to this signal via `async_dispatcher_connect` and checks whether the dismissed notification's ID corresponds to a pending ANS delivery.

### Event Payload

```json
{
    "notification_id": "<uuid-v4>",
    "channel_id": "mobile_app" | "notify.persistent_notification",
    "acknowledged_at": "2026-05-20T10:30:00+00:00",
    "action": "<mobile action id, optional>",
    "device_id": "<mobile device id suffix, optional>"
}
```

`action` and `device_id` are only included for mobile acknowledgements. Persistent notification acknowledgements include only `notification_id`, `channel_id`, and `acknowledged_at`.

### Idempotency

Each `notification_id` can be acknowledged only once. The first acknowledgement wins; subsequent acknowledgements for the same notification are silently discarded. `AcknowledgementRegistry` persists acknowledgements to `ans_acknowledgements.json` in HA `.storage/`, and they survive HA restarts. Housekeeping removes records older than the configured retention period.

### Pending-Acks Scope

ANS persists pending acknowledgement eligibility to storage and restores it on startup. This means a notification delivered before a restart can still be acknowledged after restart.

> **Note:** Acknowledgements remain idempotent per `notification_id`. The first acknowledgement wins; subsequent taps or dismissals for the same notification do not emit additional `ans_notification_acknowledged` events.

### Limitations

- Acknowledgement tracking is only supported for **Mobile App** and **Persistent Notification** channels.
- Signal, TTS, and other adapters have no interaction model that ANS can observe.

See [Channel Reference → Mobile App → `data.tag` and Acknowledgement Tracking](channels.md#datatag-and-acknowledgement-tracking) for the tag mechanism details and [Advanced → Storage Files](advanced.md#ans_acknowledgementsJSON) for storage details.

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

*Previous: [← Installation & Configuration](getting-started.md) | Next: [Channel Reference →](channels.md)*
