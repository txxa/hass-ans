# Advanced Topics

*Previous: [← Troubleshooting](troubleshooting.md)*

This section covers internals and extension points for users who want to understand ANS more deeply, inspect its storage, or work around current limitations.

## Contents

- [Delivery Snapshots and Crash Recovery](#delivery-snapshots-and-crash-recovery)
- [Storage Files Reference](#storage-files-reference)
  - [`ans.notifications`](#ansnotifications)
  - [`ans.delivery_attempts`](#ansdelivery_attempts)
  - [`ans.retry_queue`](#ansretry_queue)
  - [`ans.acknowledgements`](#ansacknowledgements)
  - [`ans.volume_restoration`](#ansvolume_restoration)
  - [Housekeeping](#housekeeping)
- [Rate Limiter — Token Bucket Details](#rate-limiter--token-bucket-details)
- [Deduplication — LRU Cache Details](#deduplication--lru-cache-details)
- [Delivery Outcome Events — Payload Reference](#delivery-outcome-events--payload-reference)
- [Channel Manager — Adapter Lifecycle](#channel-manager--adapter-lifecycle)
  - [Stale Channel Repairs Issues](#stale-channel-repairs-issues)
- [Extending ANS with a New Channel](#extending-ans-with-a-new-channel)
- [Backwards Compatibility](#backwards-compatibility)

> **Note:** Per-channel reference material (field handling, `channel_data` options, acknowledgement mechanics, limitations) has moved to [Channel Reference](channels.md).


## Delivery Snapshots and Crash Recovery

When `ans.send_notification` is called, ANS takes an immutable snapshot of the full configuration before creating any delivery tasks. Each task stores a reference to this snapshot.

If a task needs to be retried (due to rate limiting or a transient failure), the retry is scheduled as a record in `ans.retry_queue`. Each record contains the full serialized task — including payload, recipient policy, contact info, and channel info — so the task can be reconstructed without touching live configuration.

On HA startup, `PersistenceRecovery` reads the retry queue and re-enqueues all pending tasks. Tasks older than 2 hours are automatically discarded to avoid delivering stale notifications.

This means:
- A reboot mid-delivery does not lose in-flight retries
- Retries use the configuration that was active *when the notification was originally sent*, not the current configuration at retry time
- Orphaned tasks (snapshots that reference deleted recipients or channels) are detected and discarded cleanly


## Storage Files Reference

All ANS data lives in `<config>/.storage/`. The files are plain JSON and human-readable.

### `ans.notifications`

One entry per `send_notification` call. Written when audit logging is enabled.

```json
{
  "notification_id": "a1b2c3d4-...",
  "source": "automation.front_door_motion",
  "triggered_at": "2026-04-20T08:15:00+00:00",
  "payload": {
    "title": "Motion Detected",
    "message": "Front door camera triggered.",
    "type": "SECURITY",
    "criticality": "HIGH",
    "image": null,
    "video": null,
    "file": null,
    "link": null,
    "context": {},
    "channel_data": {}
  },
  "recipients": [
    {"recipient_id": "alice_id", "channels": ["notify.mobile_app_alice"]},
    {"recipient_id": "bob_id", "channels": ["notify.signal"]}
  ]
}
```

### `ans.delivery_attempts`

One entry per delivery attempt (per recipient + channel). Each retry is a separate attempt with an incremented `attempt_number`.

```json
{
  "attempt_id": "...",
  "job_id": "...",
  "notification_id": "a1b2c3d4-...",
  "channel_id": "notify.mobile_app_alice",
  "recipient_id": "alice_id",
  "attempt_number": 1,
  "started_at": "2026-04-20T08:15:01+00:00",
  "ended_at": "2026-04-20T08:15:01.250+00:00",
  "status": "SUCCESS",
  "endpoint": null,
  "remote_id": null,
  "error": null,
  "response_time_ms": 250
}
```

Possible `status` values: `SUCCESS`, `FILTERED`, `RATE_LIMITED`, `TRANSIENT_FAIL`, `PERMANENT_FAIL`, `IN_PROGRESS`.

### `ans.retry_queue`

Pending retry tasks. Each entry includes the full serialized task snapshot so it can be replayed after restart.

```json
{
  "job_id": "...",
  "notification_id": "a1b2c3d4-...",
  "scheduled_at": "2026-04-20T08:16:01+00:00",
  "reason": "rate_limited",
  "task_snapshot": { ... }
}
```


### `ans.acknowledgements`

One entry per tracked acknowledgement lifecycle record. ANS writes `pending` entries when delivery succeeds on an acknowledgeable channel and transitions them to `acknowledged` when user interaction is observed.

```json
{
  "notification_id": "a1b2c3d4-...",
  "channel_id": "notify.mobile_app_phone",
  "status": "pending",
  "delivered_at": "2026-04-20T08:18:10+00:00"
}
```

```json
{
  "notification_id": "a1b2c3d4-...",
  "channel_id": "mobile_app",
  "status": "acknowledged",
  "acknowledged_at": "2026-04-20T08:20:05+00:00"
}
```

| Field | Description |
|---|---|
| `notification_id` | UUID of the ANS notification being tracked. |
| `channel_id` | Delivery or acknowledgement channel context (`notify.mobile_app_*`, `notify.persistent_notification`, or `mobile_app` for mobile acknowledgement records). |
| `status` | `pending` (delivered, waiting for user interaction) or `acknowledged` (interaction observed and recorded). |
| `delivered_at` | ISO-8601 timestamp for pending records. |
| `acknowledged_at` | ISO-8601 timestamp of the first acknowledgement. Subsequent taps or dismissals for the same `notification_id` do not update this record. |

Records are removed by the hourly housekeeping task when they are older than `storage_retention_days`.

### `ans.volume_restoration`

Pending TTS volume-restore intents — one entry per media player currently overridden for a TTS announcement, capturing the volume level to restore once playback ends. Unlike the four files above, this one holds functional state for [TTS Volume Management](tts-volume-management.md), not an audit trail, and it exists regardless of the `enable_audit_logging` setting.

```json
{
  "intents": [
    {
      "entity_id": "media_player.living_room",
      "original_volume": 0.4,
      "override_volume": 0.8,
      "timestamp": "2026-04-20T08:15:00+00:00",
      "timeout": "2026-04-20T09:15:00+00:00"
    }
  ],
  "version": 1,
  "timestamp": "2026-04-20T08:15:00+00:00"
}
```

| Field | Description |
|---|---|
| `entity_id` | Media player entity this intent applies to. |
| `original_volume` | Volume level (0.0–1.0) to restore once TTS playback completes. |
| `override_volume` | Volume level set for the TTS announcement itself. |
| `timestamp` | ISO-8601 time the intent was captured. |
| `timeout` | ISO-8601 expiry; if playback never reaches idle before this time, the intent is dropped without restoring the volume. |

**Not covered by the hourly housekeeping task described below** — this file cleans itself independently: an internal 5-minute timer expires any intent past its `timeout`, every successful restore removes its own intent immediately, and any leftover intents from an HA restart are reconciled at startup. Intents are short-lived by design (default timeout: 1 hour), so this file does not accumulate.

### Housekeeping

ANS runs a housekeeping task hourly. It removes records older than the configured retention period (`storage_retention_days`, default 7) from the four audit-related storage files above (`ans.notifications`, `ans.delivery_attempts`, `ans.retry_queue`, `ans.acknowledgements`). Setting retention to `0` disables automatic cleanup for those four. `ans.volume_restoration` is not part of this sweep — see its own section above for how it cleans itself.


## Rate Limiter — Token Bucket Details

ANS uses a token bucket algorithm for both global and per-recipient rate limiting.

- **Capacity** = `rate_limit` (max burst)
- **Refill rate** = `rate_limit / rate_limit_window` tokens per second
- **Window** = `rate_limit_window` seconds (default 60)

When a task is rate-limited, a retry is scheduled using the same exponential backoff formula as transient failures (`retry_base_delay × backoff_factor^(attempt-1)`, capped at `retry_max_delay`). With default settings (base=60 s, factor=2), the first rate-limited retry fires after 60 seconds — matching the rate limit window by coincidence of defaults, not by a separate mechanism.

Setting `global_rate_limit` or a recipient's `rate_limit` to `0` disables that layer entirely.


## Deduplication — LRU Cache Details

The deduplication cache is an in-memory LRU structure:
- **Cache size:** 1 000 entries maximum (older entries evicted when full)
- **TTL:** 60 seconds per `(notification_id, channel_id)` entry
- **Cleanup:** A background task runs every 60 seconds to remove expired entries
- **Scope:** Per ANS instance, not persisted across restarts

The cache is reset on each HA restart. This means that if a notification was delivered just before a restart and the same notification_id is retried after restart, deduplication will not catch it. In practice this only affects tasks in the retry queue that survived a restart.


## Delivery Outcome Events — Payload Reference

Complete payload schemas for all events fired by the delivery pipeline. See [How It Works → Delivery Outcome Events](how-it-works.md#delivery-outcome-events) for a conceptual overview.

### Base payload (all per-channel events)

```json
{
    "notification_id": "<uuid-v4>",
    "recipient_id": "<recipient-id>",
    "channel_id": "<channel-id>",
    "source": "<source-string>",
    "criticality": "<criticality-level>",
    "type": "<notification-type>"
}
```

### `ans_notification_delivered`

```json
{
    "notification_id": "...",
    "recipient_id": "...",
    "channel_id": "...",
    "source": "...",
    "criticality": "...",
    "type": "...",
    "attempt_number": 1,
    "remote_id": "<adapter-provided-id-or-null>",
    "mobile_tag": "<custom-tag>"
}
```

`remote_id` is an opaque string returned by the channel adapter (e.g. a message ID from the downstream service). It is `null` when the adapter does not provide one.

`mobile_tag` is present **only** for Mobile App deliveries that used a custom `channel_data.tag` different from the notification's UUID. When no override was supplied (the common case — the tag defaults to the notification UUID), this field is omitted from the payload entirely rather than being `null`.

### `ans_notification_filtered`

```json
{
    "notification_id": "...",
    "recipient_id": "...",
    "channel_id": "...",
    "source": "...",
    "criticality": "...",
    "type": "...",
    "filter_reason": "<FilterReason-enum-value>"
}
```

### `ans_notification_failed`

```json
{
    "notification_id": "...",
    "recipient_id": "...",
    "channel_id": "...",
    "source": "...",
    "criticality": "...",
    "type": "...",
    "error": "<exception-message-or-null>",
    "attempt_number": 3
}
```

This event fires only when `PERMANENT_FAIL` is reached (all retry attempts exhausted). Transient failures during earlier attempts are logged but not surfaced as events.

### `ans_notification_rate_limited`

```json
{
    "notification_id": "...",
    "recipient_id": "...",
    "channel_id": "...",
    "source": "...",
    "criticality": "...",
    "type": "...",
    "limit_type": "GLOBAL",
    "retry_at": "2025-01-01T12:00:00+00:00"
}
```

`limit_type` is `"GLOBAL"` or `"RECIPIENT"`. `retry_at` is an ISO-8601 timestamp indicating when the task is scheduled to be retried; it is `null` when the rate-limit is terminal (retries exhausted), in which case `ans_notification_failed` is also fired.

### `ans_notification_settled`

```json
{
    "notification_id": "<uuid-v4>",
    "total_tasks": 4,
    "total_recipients": 2,
    "delivered": 3,
    "failed": 0,
    "filtered": 1,
    "recipients_delivered": 2,
    "recipients": {
        "<recipient-id-1>": {
            "channels_total": 3,
            "channels_delivered": 2,
            "channels_failed": 0,
            "channels_filtered": 1
        },
        "<recipient-id-2>": {
            "channels_total": 1,
            "channels_delivered": 1,
            "channels_failed": 0,
            "channels_filtered": 0
        }
    }
}
```

Field definitions:

| Field | Description |
|---|---|
| `total_tasks` | Total channel tasks dispatched for this notification |
| `total_recipients` | Number of unique recipient IDs addressed |
| `delivered` | Tasks that reached `SUCCESS` |
| `failed` | Tasks that reached `PERMANENT_FAIL` |
| `filtered` | Tasks dropped by a filter stage |
| `recipients_delivered` | Recipients with `channels_delivered >= 1` |
| `recipients[id].channels_total` | Total tasks for that recipient |
| `recipients[id].channels_delivered` | Successful deliveries for that recipient |
| `recipients[id].channels_failed` | Permanent failures for that recipient |
| `recipients[id].channels_filtered` | Filtered tasks for that recipient |

> **TTL edge case:** Settled tracking is held in memory with a TTL of `RCPT_MAX_RETRY_ATTEMPTS × retry_max_delay` (5 × your configured `retry_max_delay`), chosen so it always exceeds the worst-case retry schedule. With the default `retry_max_delay` of 3600s that's 18,000s (5 hours); since `retry_max_delay` is configurable (60-3600s), the actual TTL scales with it and can range from 5 minutes to 5 hours. If retries are still outstanding when the TTL expires, the settled event is not fired and a warning is logged instead.


## Channel Manager — Adapter Lifecycle

The `ChannelManager` maintains the live set of channel adapters. Understanding adapter states helps when debugging channel detection issues.

| Status | Meaning |
|---|---|
| `DETECTED` | Channel visible in HA (notify service or media player exists) but not in the `enabled_channels` system config. No adapter is created. |
| `ACTIVE` | Detected and enabled. Adapter instance exists and is ready to deliver. |
| `INACTIVE` | Listed in `enabled_channels` config and detected in HA, but no adapter implementation is registered for it. Adapter cannot be created. |
| `STALE` | Was previously known (any status); no longer detected in HA. Adapter is destroyed. |

Adapter types:
- **STATIC** (e.g., `notify.persistent_notification`) — always present, single instance
- **DYNAMIC_SINGLE** (e.g., `notify.signal`) — created once when enabled
- **DYNAMIC_MULTI** (e.g., `notify.mobile_app_*`, `media_player.*`) — one adapter instance per detected variant

### Stale Channel Repairs Issues

The `ChannelManager` exposes a lifecycle callback hook (`set_channel_lifecycle_callback`) that is invoked whenever `sync()` causes channels to transition between states. ANS uses this hook to drive the HA Repairs integration.

**Callback signature:**
```python
Callable[[list[str], list[str]], None]
# Arguments: (newly_staled_channel_ids, newly_recovered_channel_ids)
```

Both lists contain raw channel IDs (e.g. `notify.mobile_app_phone`). The callback is invoked synchronously from `sync()` and only when at least one of the two lists is non-empty.

**What ANS does with the callback:**

| Event | Action |
|---|---|
| Channel transitions to `STALE` | `ir.async_create_issue(domain, issue_id, ...)` — raises a Repairs issue with `stale_channel_<id>` as the issue key |
| Channel transitions from `STALE` to `ACTIVE` | `ir.async_delete_issue(domain, issue_id)` — dismisses the Repairs issue |

Issue IDs use `channel_id.replace(".", "_")` so they are valid HA identifiers (e.g., `stale_channel_notify_mobile_app_phone`). The original `channel_id` is preserved as a `translation_placeholder` and displayed in the issue description.

**Post-restart cleanup sweep:**
After `finalize_setup()` completes, ANS calls `ir.async_delete_issue()` for every currently `ACTIVE` channel. This is a safe no-op when no matching issue exists, but ensures any issue that was raised before a restart (when the channel was STALE) is cleaned up if the channel recovered while HA was offline. This avoids stale Repairs issues persisting across restarts.

**Translation keys:** `issues.stale_channel.title` and `issues.stale_channel.description` are defined in `translations/en.json` (and `de.json`, `fr.json`). Placeholders: `{channel_label}` (display name), `{channel_id}` (raw service ID).


## Extending ANS with a New Channel

The adapter interface is defined in `channels/base.py`. To add a new channel:

1. Create a class that extends the base adapter and implement the `deliver()` async method.
2. Define `ADAPTER_METADATA` (channel prefix, integration, adapter type).
3. Register the adapter class with the `ChannelManager`'s factory registry.

The adapter receives a `NotificationPayload`, `RecipientContactInfo`, `idempotency_key`, `job_id`, and optional `options`. It must return a `DeliveryResult` with a `DeliveryStatus`.

> This is an unofficial extension point. The adapter API is not versioned and may change between ANS releases.


## Backwards Compatibility

### `metadata` field (deprecated)

Prior to the notification payload redesign, `ans.send_notification` accepted a single flat `metadata` dict that was passed directly to channel adapters:

```yaml
# Old form — no longer recommended
service: ans.send_notification
data:
  source: "automation.front_door"
  title: "Motion detected"
  message: "Front door camera triggered."
  type: SECURITY
  criticality: HIGH
  metadata:
    text_mode: styled        # Signal: bold title formatting
    tag: motion-front-door   # Mobile App: notification tag
    entity: binary_sensor.front_door  # Persistent: context link
```

This field has been replaced by two focused fields:

- **`channel_data`** — flat dict of adapter-specific delivery overrides (e.g. `text_mode`, `tag`, `attachments`, `verify_ssl`). Each adapter reads only the keys it recognises and ignores the rest.
- **`context`** — key-value pairs for correlation and display. Persistent notifications append them as a `Context:` section; the mobile app uses the `entity` key for a tap-action deep-link; other adapters ignore all context keys.

**Migrating existing automations:**

```yaml
# New form
service: ans.send_notification
data:
  source: "automation.front_door"
  title: "Motion detected"
  message: "Front door camera triggered."
  type: SECURITY
  criticality: HIGH
  channel_data:
    text_mode: styled
    tag: motion-front-door
  context:
    entity: binary_sensor.front_door
```

**How the shim works:**

For backwards compatibility, `metadata` is still accepted. When it is present in a service call and neither `channel_data` nor `context` is explicitly provided, the metadata contents are copied into both fields as a fallback. If `channel_data` or `context` are explicitly set, they take full priority — metadata is not merged, only used when the respective field is absent.

A deprecation warning is written to the HA log each time `metadata` is used:

```
WARNING ... ANS: 'metadata' is deprecated and will be removed in a future version.
Replace it with 'channel_data' (adapter-specific options such as text_mode, tag,
attachments) and/or 'context' (correlation data such as entity, camera).
```

> **Note:** Because the same dict is applied to both `context` and `channel_data`, adapter-specific keys (e.g. `text_mode`) will also appear in the persistent notification `Context:` section. This is a known trade-off of the shim approach and does not affect delivery behaviour. Migrating to explicit `channel_data` / `context` fields eliminates this.

---

*Previous: [← Troubleshooting](troubleshooting.md)*
