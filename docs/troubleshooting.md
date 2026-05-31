# Troubleshooting & FAQ

*Previous: [← Usage Examples](usage-examples.md) | Next: [Advanced Topics →](advanced.md)*

Diagnosing common delivery problems, channel issues, and answers to frequently asked questions.

## Contents

- [Troubleshooting](#troubleshooting)
  - [Channel Not Appearing](#channel-not-appearing)
  - [Channel Went Missing — Repairs Issue](#channel-went-missing--repairs-issue)
  - [Notification Not Delivered](#notification-not-delivered)
  - [Notification Dropped — Queue Full](#notification-dropped--queue-full)
  - [DND Is Blocking Notifications I Want](#dnd-is-blocking-notifications-i-want)
  - [Rate Limit Triggered](#rate-limit-triggered)
  - [TTS Volume Not Restoring](#tts-volume-not-restoring)
  - [Mobile Notifications Missing](#mobile-notifications-missing)
  - [Signal Notifications Not Sending](#signal-notifications-not-sending)
  - [Checking the Diagnostics Panel](#checking-the-diagnostics-panel)
- [FAQ](#faq)
  - [Can I add multiple mobile devices for the same person?](#can-i-add-multiple-mobile-devices-for-the-same-person)
  - [What happens if HA restarts while a notification is being delivered?](#what-happens-if-ha-restarts-while-a-notification-is-being-delivered)
  - [Can I disable certain notification types globally?](#can-i-disable-certain-notification-types-globally)
  - [Where is the delivery history stored?](#where-is-the-delivery-history-stored)
  - [Can I use ANS without HACS?](#can-i-use-ans-without-hacs)
  - [Is my phone number stored securely?](#is-my-phone-number-stored-securely)
  - [Can two recipients share the same channel?](#can-two-recipients-share-the-same-channel)
  - [Can I configure ANS without a UI? (YAML configuration)](#can-i-configure-ans-without-a-ui-yaml-configuration)
  - [Why does my media player show up in the channel list but I can't select it for a non-TTS recipient?](#why-does-my-media-player-show-up-in-the-channel-list-but-i-cant-select-it-for-a-non-tts-recipient)

## Troubleshooting

### Channel Not Appearing

**Symptom:** A `notify.*` service or a `media_player.*` entity you expect to see in the channel list is missing.

**Causes and fixes:**

1. **Integration not yet loaded when ANS started** — call `ans.refresh_channels` to trigger a manual re-scan:
   ```yaml
   service: ans.refresh_channels
   ```

2. **TTS service not configured** — media player entities only appear as channels when a TTS service is selected in system settings. Go to **Settings → Integrations → ANS → Reconfigure** and select your TTS service.

3. **Media player missing required features** — ANS only detects media players that support both `PLAY_MEDIA` and `VOLUME_SET`. Check that your media player integration reports these capabilities. Players that are grouped, virtual, or stream-only may not qualify.

4. **Channel not enabled** — a channel must be in the "Enabled notification channels" list (system settings) before it can be assigned to recipients. Check **Settings → Integrations → ANS → Reconfigure** and confirm the channel is selected.

---

### Channel Went Missing — Repairs Issue

**Symptom:** A notification channel that was working suddenly disappears, and you see a **Repairs** issue in **Settings → System → Repairs** with the title "Notification channel unavailable: {channel}".

**What happened:** The `notify.*` service or `media_player.*` entity that this channel was backed by is no longer available in Home Assistant. This happens when the Companion App is uninstalled, an integration is removed, or an entity is renamed. ANS has marked the channel as `STALE` and raised a Repairs issue to surface the problem.

**Consequences:** Any delivery tasks targeting this channel will result in `PERMANENT_FAIL` until the channel is restored.

**Resolution options:**

1. **Restore the channel** — reinstall the Companion App, re-add the integration, or restore the entity name. Then run `ans.refresh_channels` to re-detect it:
   ```yaml
   service: ans.refresh_channels
   ```
   The Repairs issue is dismissed automatically once ANS detects the channel as active again.

2. **Remove the channel from recipient mappings** — if the channel is gone permanently, go to each affected recipient's Channel Mapping and replace the stale channel with an available one. Then run `ans.refresh_channels` to clear the STALE status.

> **Note:** ANS runs a Repairs cleanup sweep on every startup. If the missing channel was restored while Home Assistant was offline, the issue will be dismissed automatically the next time ANS loads.

---

### Notification Not Delivered

**Symptom:** You called `ans.send_notification` but a recipient did not receive anything.

**Step 1 — Check the audit log:**

Open `<config>/.storage/ans_delivery_attempts.json`. Find the attempt for your notification and read the `status` and `error` fields.

| Status | Meaning |
|---|---|
| `FILTERED` | Blocked by type filter, source regex, or DND. Not a delivery failure — this is expected behavior. |
| `RATE_LIMITED` | Token bucket empty; delivery was scheduled for retry. |
| `PERMANENT_FAIL` | Non-retryable failure (e.g. missing contact info). Check `error` field. |
| `TRANSIENT_FAIL` | Delivery error, retry was scheduled. Check `error` for the root cause. |
| `SUCCESS` | Delivered successfully. If you didn't receive it, the issue is downstream (device, app, channel). |

**Step 2 — Narrow down the cause:**

- **`FILTERED` with reason `TYPE_NOT_ALLOWED`** — the recipient's notification type allow-list doesn't include the type you sent. Update the recipient's Basic Settings.
- **`FILTERED` with reason `DND_ACTIVE`** — the notification arrived during quiet hours and didn't match any bypass rule. Adjust DND bypass settings if needed.
- **`FILTERED` with reason `SOURCE_BLOCKED`** — the `source` field matched the recipient's blocked sources regex. Check the pattern in Basic Settings.
- **`PERMANENT_FAIL`** — most commonly means a phone number is required but not set, or the channel adapter was permanently rejected by the service.
- **`TRANSIENT_FAIL` repeatedly** — the underlying service (mobile app, Signal API) may be unavailable. Check that integration in HA.

**Step 3 — Check criticality mapping:**

Confirm the criticality level you used maps to at least one channel for that recipient. In the recipient's Channel Mapping, a criticality level with no channels configured causes silent suppression.

---

### Notification Dropped — Queue Full

**Symptom:** An `ans_notification_failed` event fires with `error: "queue_full"` immediately after `ans.send_notification`, and a warning appears in the HA log: `Delivery queue is full`.

**What happened:** The ANS delivery queue reached its configured maximum depth (`queue_max_depth`, default 500). The incoming task was dropped rather than queued to prevent unbounded memory growth. This typically indicates a misconfigured automation firing at a very high rate.

**Resolution options:**

1. **Fix the runaway automation** — inspect recent automations for loops or high-frequency triggers. An automation that calls `ans.send_notification` in response to every state change of a frequently-updating entity (e.g., a sensor that changes every second) is the most common cause.

2. **Increase `queue_max_depth`** — if the volume is legitimate (many recipients, many channels, burst notifications), go to **Settings → Integrations → ANS → Configure** and increase **Queue max depth** (maximum: 5 000). Monitor memory usage after increasing.

3. **Reduce `queue_max_concurrency`** — a lower concurrency setting means tasks drain from the queue more slowly, which can exacerbate queue-full events. If you increased concurrency previously and now see queue-full drops, try lowering it.

> **Note:** Tasks that are in the persistent retry queue and cannot be re-enqueued because the queue is full are **not** discarded — they are deferred to the next retry cycle (every 10 seconds). Only brand-new tasks from `ans.send_notification` are dropped when the queue is full.

---

### DND Is Blocking Notifications I Want

**Symptom:** A notification you expected to get through was suppressed during quiet hours.

**Check these bypass settings** in the recipient's DND configuration:

- Is the notification's `criticality` in **"Criticality levels that bypass DND"**? Default: only `CRITICAL`.
- Is the notification's `type` in **"Notification types that bypass DND"**? Default: only `ALERT`.
- Does the notification's `source` match **"Allowed sources during DND (regex)"**?

**Common fix:** Add `SECURITY` to the type bypass list, or `HIGH` to the criticality bypass list, depending on what you want to allow.

---

### Rate Limit Triggered

**Symptom:** Deliveries are being scheduled as `RATE_LIMITED`. Notifications are delayed.

**Diagnose:**

- If it's happening globally (across all recipients), your `global_rate_limit` in Options may be too low. Increase it or set it to `0` to disable global limiting.
- If it's happening for one recipient, that recipient's per-recipient `rate_limit` is being hit. Update it in Basic Settings, or reduce the frequency of notifications from that source.

**Default limits:**
- Global: 100 notifications/minute
- Per-recipient: 20 notifications/minute

Rate-limited tasks are automatically retried once the bucket refills (within the next rate limit window, default 60 seconds).

---

### TTS Notification Not Delivered to Standby Device

**Symptom:** A TTS notification is not spoken on a Google Nest or Cast device that is in standby (the device reports `off` in HA).

**What ANS does:** ANS delivers `tts.speak` directly to standby (`off`) devices without blocking on their state. Platforms like Google Cast handle wakeup natively when they receive a play command, so the device should wake up and announce. Volume management is skipped for standby devices — the announcement plays at the device's own wake-up volume.

**If delivery still fails:** The attempt log will show `TRANSIENT_FAIL` with an error from the TTS service call (e.g. `TTS service call failed`). This means the device did not accept the command. Possible causes:

- The device is fully powered off (not just standby) and cannot receive network commands.
- The media player entity is `unavailable` in HA — check that the underlying integration (e.g. Google Cast) is running and the device is on the same network.
- The TTS service itself failed (check the HA log for errors from your TTS engine).

ANS will retry automatically with exponential backoff.

---

### TTS Volume Not Restoring

**Symptom:** After a TTS announcement, the media player's volume stays at the announcement level instead of returning to the original.

**Causes and fixes:**

1. **Media player never reached IDLE state** — ANS waits for the player to become idle before restoring. If playback hangs or the player transitions to a different state, restoration may not trigger immediately. A dynamic fallback timer (estimated from message length, capped at 120 seconds) will fire as a safety net.

2. **Volume was manually adjusted during playback** — ANS detects manual volume changes (with a 5-second guard window) and aborts restoration to avoid overriding user intent.

3. **HA restarted during playback** — Volume restoration state is persisted across restarts. ANS will attempt restoration when it loads and detects the pending intent.

4. **Volume management disabled** — Confirm **"Enable Volume Management"** is checked in the TTS recipient's TTS settings.

5. **Device was in standby when notified** — Standby (`off`) devices skip volume management by design. Volume is not captured or restored for these devices — the announcement plays at the device's own wake-up level.

---

### Mobile Notifications Missing

**Symptom:** An HA User recipient doesn't receive mobile push notifications.

**Checks:**

1. The recipient's channel mapping for the relevant criticality level includes `notify.mobile_app_{device_id}`.
2. The mobile device is the one linked to the HA user account for this recipient.
3. The HA Companion App is installed, logged in, and notifications are enabled in the device's OS settings.
4. If you recently added a new phone, call `ans.refresh_channels` so ANS picks up the new `notify.mobile_app_*` service.

**Multiple devices for one user:** HA User recipients support multiple `notify.mobile_app_*` channels. To notify more than one device, assign all relevant `notify.mobile_app_*` channels in the recipient's Channel Mapping step. Ensure all of them are included in the system-wide **Enabled notification channels** list first. Creating a Generic recipient will not work for mobile app channels because Generic recipients are not linked to an HA user account, which is required for `notify.mobile_app_*` channel availability.

---

### Signal Notifications Not Sending

**Symptom:** Signal delivery fails with an error in the attempt log.

**Checks:**

1. The Signal integration is configured and working in HA (test with a direct `notify.signal` call from Developer Tools).
2. The recipient has a phone number in E.164 format (e.g., `+1234567890`).
3. `notify.signal` is in the enabled channels list (system settings) and mapped to the relevant criticality levels for this recipient.

**Attachments not arriving:**
If the message delivers but an expected file attachment is missing, the path may have been rejected by the attachment path guard. ANS only allows paths inside the HA `config/`, `media/`, or `www/` directories. Check the HA log for a warning like `Signal attachment rejected (path outside allowed directories)`. Move the file to an allowed directory (e.g., `/media/snapshots/`) and update the automation. See [Signal Messenger — `channel_data` Reference](advanced.md#signal-messenger--channel_data-reference).

---

### Checking the Diagnostics Panel

The HA Diagnostics panel provides a non-PII health snapshot of the ANS system:

1. Go to **Settings → Devices & Services → Advanced Notification System**.
2. Click the three-dot menu (⋮) and select **Download Diagnostics**.

The report includes:
- Total detected and active channels, broken down by scope (system, recipient, TTS)
- Per-channel: ID, label, scope, integration, status, adapter availability
- Recipient count by type (no names, emails, or phone numbers)
- Enabled channels from system config

Use this to verify that expected channels are `ACTIVE` and that adapters are available.

---

## FAQ

### Can I add multiple mobile devices for the same person?

Yes. An HA User recipient can have multiple `notify.mobile_app_*` channels assigned across their criticality levels. Simply ensure all relevant `notify.mobile_app_*` services are included in the system-wide **Enabled notification channels** list, then assign them in the recipient's Channel Mapping step. All mapped channels receive the notification independently.

---

### What happens if HA restarts while a notification is being delivered?

Pending retries and in-progress tasks are persisted to `<config>/.storage/ans_retry_queue.json`. On the next startup, ANS loads this queue and re-schedules any tasks that were waiting for retry. Tasks that were actively mid-delivery when HA shut down may not have a saved attempt record; they will be retried at startup if their retry schedule was already persisted.

Notifications in the retry queue older than 2 hours are automatically discarded on startup to prevent stale alerts.

---

### Can I disable certain notification types globally?

No global type filter exists. Type filtering is per-recipient only. If you want to suppress a type across all recipients, you would need to update each recipient's **"Notification types to receive"** list. Alternatively, use `blocked_sources_regex` on a per-recipient basis if the notifications come from identifiable sources.

---

### Where is the delivery history stored?

In the HA `.storage/` directory:
- `ans_notifications.json` — one entry per `send_notification` call
- `ans_delivery_attempts.json` — one entry per delivery attempt (per recipient + channel)
- `ans_retry_queue.json` — pending retry tasks

These are plain JSON files. Retention is controlled by the **Audit log retention** setting in Options (default: 7 days).

---

### Can I use ANS without HACS?

Yes. Download `ans.zip` from the [GitHub releases page](https://github.com/txxa/hass-ans/releases), extract it, and copy the `ans/` folder to `custom_components/`. See [Installation](getting-started.md#manual-installation) for full steps.

---

### Is my phone number stored securely?

Phone numbers are stored as part of the HA config entry in the standard HA configuration storage (`.storage/core.config_entries`). They are protected by the same access controls as the rest of your HA configuration. ANS does not transmit contact data externally — delivery goes through the HA `notify.signal` service, which is your locally configured Signal integration.

---

### Can two recipients share the same channel?

Yes. Multiple recipients can have the same channel (e.g., `notify.mobile_app_family_tablet`) in their channel mapping. ANS creates independent delivery tasks for each recipient and deduplication is keyed per `(notification_id, channel_id)` — so if two recipients map to the same channel, the notification is delivered twice (once per recipient). This is intentional: each recipient is tracked independently in the audit log.

---

### Can I configure ANS without a UI? (YAML configuration)

No. ANS uses the HA config flow UI exclusively. YAML-based configuration is not supported. All settings — system, options, and recipients — are managed through **Settings → Devices & Services**.

---

### Why does my media player show up in the channel list but I can't select it for a non-TTS recipient?

Media player channels (`media_player.*`) are reserved for TTS recipients only. For non-TTS recipients (HA User, Generic, System), only `notify.*` channels are shown. This is by design: sending text to a media player requires a TTS engine, which is configured on TTS recipient types.

---

*Previous: [← Usage Examples](usage-examples.md) | Next: [Advanced Topics →](advanced.md)*
