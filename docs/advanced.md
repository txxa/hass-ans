# Advanced Topics

*Previous: [← Troubleshooting](troubleshooting.md)*


This section covers internals and extension points for users who want to understand ANS more deeply, inspect its storage, or work around current limitations.

## Contents

- [Delivery Snapshots and Crash Recovery](#delivery-snapshots-and-crash-recovery)
- [Storage Files Reference](#storage-files-reference)
  - [`ans_notifications.json`](#ans_notificationsjson)
  - [`ans_delivery_attempts.json`](#ans_delivery_attemptsjson)
  - [`ans_retry_queue.json`](#ans_retry_queuejson)
  - [Housekeeping](#housekeeping)
- [Signal Messenger — Metadata Reference](#signal-messenger--metadata-reference)
- [TTS — SSML Mode](#tts--ssml-mode)
- [TTS — Volume Restoration Registry](#tts--volume-restoration-registry)
- [Rate Limiter — Token Bucket Details](#rate-limiter--token-bucket-details)
- [Deduplication — LRU Cache Details](#deduplication--lru-cache-details)
- [Delivery Outcome Events — Payload Reference](#delivery-outcome-events--payload-reference)
- [Channel Manager — Adapter Lifecycle](#channel-manager--adapter-lifecycle)
- [Extending ANS with a New Channel](#extending-ans-with-a-new-channel)


## Delivery Snapshots and Crash Recovery

When `ans.send_notification` is called, ANS takes an immutable snapshot of the full configuration before creating any delivery tasks. Each task stores a reference to this snapshot.

If a task needs to be retried (due to rate limiting or a transient failure), the retry is scheduled as a record in `ans_retry_queue.json`. Each record contains the full serialized task — including payload, recipient policy, contact info, and channel info — so the task can be reconstructed without touching live configuration.

On HA startup, `PersistenceRecovery` reads the retry queue and re-enqueues all pending tasks. Tasks older than 2 hours are automatically discarded to avoid delivering stale notifications.

This means:
- A reboot mid-delivery does not lose in-flight retries
- Retries use the configuration that was active *when the notification was originally sent*, not the current configuration at retry time
- Orphaned tasks (snapshots that reference deleted recipients or channels) are detected and discarded cleanly


## Storage Files Reference

All ANS data lives in `<config>/.storage/`. The files are plain JSON and human-readable.

### `ans_notifications.json`

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
    "metadata": {}
  },
  "recipients": [
    {"recipient_id": "alice_id", "channels": ["notify.mobile_app_alice"]},
    {"recipient_id": "bob_id", "channels": ["notify.signal"]}
  ]
}
```

### `ans_delivery_attempts.json`

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

### `ans_retry_queue.json`

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

### Housekeeping

ANS runs a housekeeping task hourly. It removes records older than the configured retention period (`storage_retention_days`, default 7). Setting retention to `0` disables automatic cleanup.


## Signal Messenger — Metadata Reference

The Signal adapter (`channels/signal.py`) supports the following `metadata` keys:

| Key | Type | Description |
|---|---|---|
| `text_mode` | `"styled"` \| `"normal"` | `styled` uses Signal's markdown-like formatting (`**bold**`, `_italic_`). When not set and a title is present, ANS automatically uses `styled`. |
| `attachments` | `list[str]` | Local file paths to attach (e.g., `["/media/snapshots/camera.jpg"]`). Files must be accessible by the HA process. |
| `urls` | `list[str]` | Image URLs to send as attachments. |
| `verify_ssl` | `bool` | Whether to verify SSL certificates when fetching image URLs. Default: `true`. Set to `false` for self-signed certificates. |

Phone number masking: ANS logs only the last 4 digits of phone numbers at debug level (`****1234`). Full numbers are never written to logs.


## TTS — SSML Mode

When **Enable SSML Mode** is on in TTS recipient settings, ANS wraps the message in an SSML `<speak>` document and XML-escapes all user content before sending. This enables prosody markup, pauses, and emphasis in TTS engines that support SSML.

**Enable only for SSML-capable engines:**
- Google Cloud TTS
- Amazon Polly
- Azure TTS
- edge-tts
- Piper (in SSML mode)

Do **not** enable for plain-text engines (Google Translate TTS, built-in macOS/Windows TTS via HA). Plain-text engines will speak the raw XML tags aloud.


## TTS — Volume Restoration Registry

The volume restoration registry (`persistence/volume_restoration.py`) handles the restore-after-playback lifecycle. Key behaviors:

- **State persisted across restarts** — stored under the `ans_volume_restoration` key in HA storage. If HA restarts between the TTS call and the IDLE event, restoration is still attempted on the next startup.
- **User-change detection** — if the volume is manually adjusted within 5 seconds of ANS setting it (echo-guard window), ANS treats it as intentional and aborts restoration.
- **Timeout expiry** — restoration intents expire after 1 hour. If a media player never reaches IDLE within that window, the intent is discarded.
- **120-second fallback** — a safety timer fires 120 seconds after playback starts as a fallback for players that don't reliably signal IDLE.
- **Per-device lock** — concurrent TTS requests to the same media player are serialized via a per-entity async lock. Lock acquisition has a 60-second timeout to prevent deadlock if a previous delivery hung.

**Known limitation:** The time-of-day volume windows (06:00, 09:00, 19:00, 22:00) are fixed and not user-configurable. The volumes at each window are configurable; only the window boundaries are not.


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
    "type": "...",
    "attempt_number": 1,
    "remote_id": "<adapter-provided-id-or-null>"
}
```

`remote_id` is an opaque string returned by the channel adapter (e.g. a message ID from the downstream service). It is `null` when the adapter does not provide one.

### `ans_notification_filtered`

```json
{
    "notification_id": "...",
    "recipient_id": "...",
    "channel_id": "...",
    "source": "...",
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

> **TTL edge case:** Settled tracking is held in memory with a 1-hour TTL. If retries are still outstanding when the TTL expires (e.g. a task is stuck in the retry queue for over an hour), the settled event is not fired and a warning is logged instead.


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


## Extending ANS with a New Channel

The adapter interface is defined in `channels/base.py`. To add a new channel:

1. Create a class that extends the base adapter and implement the `deliver()` async method.
2. Define `ADAPTER_METADATA` (channel prefix, integration, adapter type).
3. Register the adapter class with the `ChannelManager`'s factory registry.

The adapter receives a `NotificationPayload`, `RecipientContactInfo`, `idempotency_key`, `job_id`, and optional `options`. It must return a `DeliveryResult` with a `DeliveryStatus`.

> This is an unofficial extension point. The adapter API is not versioned and may change between ANS releases.

---

*Previous: [← Troubleshooting](troubleshooting.md)*
