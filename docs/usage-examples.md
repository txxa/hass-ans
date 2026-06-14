# Usage Examples

*Previous: [← Channel Reference](channels.md) | Next: [Troubleshooting →](troubleshooting.md)*

Practical `ans.send_notification` examples covering every channel type, common household scenarios, DND patterns, and source blocking.

## Contents

- [Example Household Setup](#example-household-setup)
- [Your First Notification](#your-first-notification)
- [Criticality-Based Routing (the Core Concept)](#criticality-based-routing-the-core-concept)
- [Scenario Library](#scenario-library)
  - [Security & Safety](#security--safety)
  - [Home Automation](#home-automation)
  - [Reminders & Schedules](#reminders--schedules)
  - [Multi-Person Household (Full Fan-out Example)](#multi-person-household-full-fan-out-example)
- [Channel-Specific Examples](#channel-specific-examples)
  - [Signal Messenger](#signal-messenger)
  - [TTS via Media Player](#tts-via-media-player)
  - [Mobile App — Delivery Overrides](#mobile-app--delivery-overrides)
  - [Persistent Notification — Context in Sidebar](#persistent-notification--context-in-sidebar)
- [Actionable Notifications (Mobile App)](#actionable-notifications-mobile-app)
  - [Primary Example: Garage Door — Tap to Close](#primary-example-garage-door--tap-to-close)
  - [Secondary Example: Unknown Person at Door — Unlock or Ignore](#secondary-example-unknown-person-at-door--unlock-or-ignore)
- [Acknowledgement Tracking](#acknowledgement-tracking)
  - [Example 1 — Cancel escalation on acknowledgement](#example-1--cancel-escalation-on-acknowledgement)
  - [Example 2 — Override the mobile tag via channel_data](#example-2--override-the-mobile-tag-via-channel_data)
- [Do Not Disturb Patterns](#do-not-disturb-patterns)
  - [Pattern 1 — Basic Quiet Hours (REMINDER silenced at midnight)](#pattern-1--basic-quiet-hours-reminder-silenced-at-midnight)
  - [Pattern 2 — Security Always Gets Through](#pattern-2--security-always-gets-through)
  - [Pattern 3 — CRITICAL Only at Night](#pattern-3--critical-only-at-night)
- [Source Blocking in Practice](#source-blocking-in-practice)
- [Reacting to Delivery Outcomes](#reacting-to-delivery-outcomes)
- [Maintaining Your Setup](#maintaining-your-setup)
  - [When to Refresh Channels](#when-to-refresh-channels)

## Example Household Setup

All examples use `ans.send_notification`. For full parameter reference see [How It Works](how-it-works.md).

The examples assume a household with these recipients configured:

| Recipient | Type | Channels (LOW) | Channels (MEDIUM) | Channels (HIGH) | Channels (CRITICAL) |
|---|---|---|---|---|---|
| **HA Instance** | HA System | `persistent_notification` | `persistent_notification` | `persistent_notification` | `persistent_notification` |
| **Alice** | HA User | — | `mobile_app_alice` | `mobile_app_alice` | `mobile_app_alice`, `signal` |
| **Bob** | Generic (+phone) | — | `signal` | `signal` | `signal` |
| **Living Room** | TTS | — | `media_player.living_room` | `media_player.living_room` | `media_player.living_room`, `media_player.bedroom` |

> **Note:** `notify.persistent_notification` is exclusively available to the **HA System** recipient (`Home Assistant (System)` type). It cannot be assigned to HA User, Generic, or TTS recipients.

## Your First Notification

The minimal working call. Every field is required.

```yaml
service: ans.send_notification
data:
  source: "my_first_test"      # Free-form label — used for source-blocking filters
  title: "Hello from ANS"      # Subject line
  message: "ANS is working."   # Body
  type: INFO                   # Category — controls type-based filtering
  criticality: LOW             # Priority — determines which channels are used
```

**What happens:**
- HA Instance sidebar receives the notification (persistent notification at every criticality level)
- Alice receives nothing (no channels mapped to LOW for HA User recipients in this example)
- Bob receives nothing (no channels mapped to LOW for Generic recipients in this example)
- Living Room speaker is silent (no channels mapped to LOW for TTS recipients in this example)

> **Tip:** Use Developer Tools → Services to run this manually and verify ANS is set up correctly before building automations.


## Criticality-Based Routing (the Core Concept)

The same event sent at different criticality levels triggers different channels. This escalation pattern — start quiet, get louder over time — is one of the most common ANS use cases.

**Scenario:** Garage door left open. Notify gently at first; escalate if it's still open.

```yaml
# automation.yaml

- alias: "Garage door left open — notify"
  trigger:
    - platform: state
      entity_id: cover.garage_door
      to: "open"
      for: "00:05:00"     # Open for 5 minutes
  action:
    - service: ans.send_notification
      data:
        source: "automation.garage_door_open"
        title: "Garage Door Open"
        message: "The garage door has been open for 5 minutes."
        type: WARNING
        criticality: LOW    # Sidebar only — gentle reminder

    - wait_template: "{{ is_state('cover.garage_door', 'closed') }}"
      timeout: "00:10:00"   # Wait up to 10 more minutes
      continue_on_timeout: true

    - condition: state
      entity_id: cover.garage_door
      state: "open"         # Still open after 15 min total

    - service: ans.send_notification
      data:
        source: "automation.garage_door_open"
        title: "Garage Door Still Open"
        message: "The garage door has been open for 15 minutes. Please check."
        type: ALERT
        criticality: HIGH   # Mobile push for Alice, Signal for Bob

    - wait_template: "{{ is_state('cover.garage_door', 'closed') }}"
      timeout: "00:15:00"
      continue_on_timeout: true

    - condition: state
      entity_id: cover.garage_door
      state: "open"         # Still open after 30 min total

    - service: ans.send_notification
      data:
        source: "automation.garage_door_open"
        title: "URGENT: Garage Door Open"
        message: "The garage door has been open for 30 minutes. Immediate action needed."
        type: ALERT
        criticality: CRITICAL  # All channels: mobile, Signal, and speakers
```

**Result with the example household:**

| Criticality | HA Instance | Alice | Bob | Living Room Speaker |
|---|---|---|---|---|
| LOW | sidebar notification | silent | silent | silent |
| HIGH | sidebar notification | mobile push | Signal message | silent |
| CRITICAL | sidebar notification | mobile push + Signal | Signal message | spoken announcement |


## Scenario Library

### Security & Safety

#### Motion detected (with camera snapshot)

```yaml
service: ans.send_notification
data:
  source: "automation.front_door_motion"
  title: "Motion at Front Door"
  message: "Motion was detected at the front door."
  type: SECURITY
  criticality: HIGH
  image: "https://your-ha-instance/api/camera_proxy/camera.front_door"
  channel_data:
    verify_ssl: true
```

Signal recipients receive the image URL as an attachment. Mobile app recipients receive it as a rich push notification image.


#### Smoke / CO alarm triggered

```yaml
service: ans.send_notification
data:
  source: "automation.smoke_alarm"
  title: "Smoke Alarm Triggered"
  message: "Smoke detected in the kitchen. Please evacuate."
  type: SECURITY
  criticality: CRITICAL
```

CRITICAL criticality bypasses DND by default (`dnd_allowed_criticalities: [CRITICAL]` is the out-of-the-box default). Every recipient on every channel is notified immediately, regardless of the time.


#### Water leak detected

```yaml
service: ans.send_notification
data:
  source: "automation.water_leak_sensor"
  title: "Water Leak Detected"
  message: "Water leak sensor triggered under the kitchen sink."
  type: ALERT
  criticality: CRITICAL
```


#### Door unlocked overnight

```yaml
service: ans.send_notification
data:
  source: "automation.front_door_lock"
  title: "Front Door Unlocked"
  message: "The front door is still unlocked at {{ now().strftime('%H:%M') }}."
  type: SECURITY
  criticality: HIGH
```

With DND enabled on recipients, this would normally be suppressed overnight. To ensure this notification gets through during quiet hours, add `SECURITY` to the recipient's **"Notification types that bypass DND"** list (the default bypass list only includes `ALERT`).


### Home Automation

#### Washing machine done

```yaml
service: ans.send_notification
data:
  source: "automation.washing_machine"
  title: "Washing Done"
  message: "The washing machine has finished its cycle."
  type: EVENT
  criticality: LOW
```

LOW criticality → persistent notification only. No phone buzz, no spoken announcement.


#### Package at the door (video doorbell)

```yaml
service: ans.send_notification
data:
  source: "automation.front_doorbell"
  title: "Someone at the Door"
  message: "Motion detected at the front door camera."
  type: EVENT
  criticality: MEDIUM
  image: "/api/camera_proxy/camera.front_door"
```

MEDIUM criticality → Alice gets a mobile push with the camera snapshot inline (`image` field). Bob gets a Signal message with the image as an attachment. A single top-level `image` field serves all channels.


#### Energy usage spike

```yaml
service: ans.send_notification
data:
  source: "automation.energy_monitor"
  title: "High Energy Usage"
  message: "Current power draw is {{ states('sensor.power_meter') | int }} W — above your 3000 W threshold."
  type: WARNING
  criticality: MEDIUM
```


#### Car left unlocked at night

```yaml
- alias: "Car unlocked at night — escalate"
  trigger:
    - platform: time
      at: "23:00:00"
  condition:
    - condition: state
      entity_id: binary_sensor.car_lock
      state: "unlocked"
  action:
    - service: ans.send_notification
      data:
        source: "automation.car_lock_check"
        title: "Car Unlocked"
        message: "Your car appears to be unlocked. Check before bed."
        type: WARNING
        criticality: HIGH
```


### Reminders & Schedules

#### Medication reminder

```yaml
- alias: "Evening medication reminder"
  trigger:
    - platform: time
      at: "20:00:00"
  action:
    - service: ans.send_notification
      data:
        source: "automation.medication_reminder"
        title: "Medication Reminder"
        message: "Time to take your evening medication."
        type: REMINDER
        criticality: MEDIUM
```

Only recipients who have `REMINDER` in their **notification types to receive** list will receive this. Recipients who opted out of reminders are not bothered.


#### Garbage collection day

```yaml
- alias: "Garbage day reminder"
  trigger:
    - platform: time
      at: "07:30:00"
  condition:
    - condition: template
      value_template: "{{ now().weekday() == 1 }}"   # Tuesday
  action:
    - service: ans.send_notification
      data:
        source: "automation.garbage_day"
        title: "Garbage Day"
        message: "Bins need to go out today."
        type: REMINDER
        criticality: LOW
```

LOW → sidebar only. No disruption, just a visual note for whoever checks the HA dashboard.


#### Morning TTS briefing

```yaml
- alias: "Morning briefing via speaker"
  trigger:
    - platform: time
      at: "07:15:00"
  action:
    - service: ans.send_notification
      data:
        source: "automation.morning_briefing"
        title: "Good Morning"
        message: >
          Good morning. It is {{ now().strftime('%A, %B %-d') }}.
          The temperature outside is {{ states('sensor.outdoor_temperature') }} degrees.
        type: INFO
        criticality: MEDIUM
```

The Living Room TTS recipient picks this up at MEDIUM criticality. ANS sets the morning volume (default 65) before speaking, then restores the original volume afterwards. No automation code needed for volume management.


### Multi-Person Household (Full Fan-out Example)

This example shows a single service call fanning out differently to three recipients based on their individual configurations.

**Setup summary:**
- **Alice** (HA User): DND 23:00–07:00, `CRITICAL` bypasses DND. Channels: LOW→sidebar, MEDIUM→mobile, HIGH→mobile, CRITICAL→mobile+Signal
- **Bob** (Generic, has phone): No DND. Channels: LOW→sidebar, MEDIUM→Signal, HIGH→Signal, CRITICAL→Signal
- **Living Room** (TTS): No DND. Channels: HIGH→living room speaker, CRITICAL→living room+bedroom speakers

```yaml
- alias: "Security breach — alert everyone"
  trigger:
    - platform: state
      entity_id: alarm_control_panel.home
      to: "triggered"
  action:
    - service: ans.send_notification
      data:
        source: "alarm_control_panel.home"
        title: "Security Alert"
        message: "Alarm triggered. Check your home immediately."
        type: SECURITY
        criticality: CRITICAL
```

**What each recipient receives:**

| Recipient | Channel | Notes |
|---|---|---|
| Alice | `notify.mobile_app_alice` | Push notification, bypasses her 23:00–07:00 DND because criticality is CRITICAL |
| Alice | `notify.signal` | Signal message to Alice's phone |
| Bob | `notify.signal` | Signal message to Bob's phone |
| Living Room | `media_player.living_room` | TTS at override volume (e.g. 80) regardless of current time |
| Living Room | `media_player.bedroom` | TTS in bedroom as well (CRITICAL maps to both speakers) |

One service call. Five deliveries. No automation logic for routing.


## Channel-Specific Examples

### Signal Messenger

#### Styled text (bold title)

```yaml
service: ans.send_notification
data:
  source: "automation.update_available"
  title: "HA Update Available"
  message: "Home Assistant 2026.5.0 is ready to install."
  type: INFO
  criticality: MEDIUM
  channel_data:
    text_mode: "styled"   # Title rendered as **bold**, message in italic
```

When `text_mode` is `styled` (or when a title is present without an explicit mode), ANS automatically renders the title as `**bold**`.

#### File attachment (local snapshot)

```yaml
service: ans.send_notification
data:
  source: "automation.door_camera"
  title: "Front Door Activity"
  message: "A snapshot was captured."
  type: EVENT
  criticality: MEDIUM
  file: "/media/snapshots/front_door.jpg"   # Must be inside config/, media/, or www/
```

> **Note:** Only paths inside the HA `config/`, `media/`, or `www/` directories are accepted. Paths outside those directories are silently dropped. See [Signal Messenger — Attachment Path Restriction](channels.md#attachment-path-restriction).

#### Image URL with SSL verification

```yaml
service: ans.send_notification
data:
  source: "automation.camera_motion"
  title: "Motion Detected"
  message: "Camera captured motion in the driveway."
  type: SECURITY
  criticality: HIGH
  image: "https://your-ha-instance.local/api/camera_proxy/camera.driveway"
  channel_data:
    verify_ssl: false   # Set true for publicly trusted certificates
```


### TTS via Media Player

#### Basic announcement

The spoken output depends on the `message_format` configured for the TTS recipient:

```yaml
# message_format: "title_and_message" (default)
# → speaks: "Doorbell. Someone is at the front door."
service: ans.send_notification
data:
  source: "automation.doorbell"
  title: "Doorbell"
  message: "Someone is at the front door."
  type: EVENT
  criticality: HIGH
```

```yaml
# message_format: "message_only" set in recipient TTS config
# → speaks: "Someone is at the front door."
service: ans.send_notification
data:
  source: "automation.doorbell"
  title: "Doorbell"
  message: "Someone is at the front door."
  type: EVENT
  criticality: HIGH
```

> **Tip:** Set `message_format` to `message_only` in the TTS recipient's Step 4 settings for announcements where the title would be redundant or awkward when spoken aloud. `title_and_message` (default) joins them as `"{title}. {message}"`. Passing an empty `title: ""` with the default format produces a leading period — always set `message_only` when you don't need a spoken title.

Volume, timing, and restoration are fully automatic based on TTS recipient configuration. No additional YAML needed.

#### Criticality-based volume

The TTS recipient is configured with:
- Daytime volume: 60
- Override volume: 90
- Override criticalities: `CRITICAL`

```yaml
# MEDIUM/HIGH → speaks at 60 (daytime volume)
service: ans.send_notification
data:
  source: "automation.reminder"
  title: "Reminder"
  message: "Your 3 PM meeting starts in 10 minutes."
  type: REMINDER
  criticality: MEDIUM

# CRITICAL → speaks at 90 (override volume), regardless of time of day
service: ans.send_notification
data:
  source: "alarm_system"
  title: "Alarm Triggered"
  message: "Security alarm has been triggered. Please check your home."
  type: SECURITY
  criticality: CRITICAL
```

No automation changes are needed when switching between levels — volume behavior is entirely driven by recipient configuration.


### Mobile App — Delivery Overrides

ANS flat-merges the `channel_data` dict into the `data:` payload sent to `notify.mobile_app_*`. All standard HA Companion App features work:

```yaml
service: ans.send_notification
data:
  source: "automation.update_check"
  title: "Update Ready"
  message: "Home Assistant 2026.5.0 is ready to install."
  type: INFO
  criticality: MEDIUM
  channel_data:
    channel: "updates"         # Android notification channel
    importance: low            # Android importance level
```

> **Note:** ANS always sets `data.tag` on every Mobile App notification for acknowledgement tracking. The tag defaults to the `notification_id` UUID and can be customised via `channel_data.tag`. Use `channel_data.tag` when you need to control the tag for notification grouping or replacement:
>
> ```yaml
> channel_data:
>   tag: "ha_update"   # Used for notification grouping/replacement and acknowledgement
> ```


### Persistent Notification — Context in Sidebar

For the HA Instance (persistent notification channel), ANS appends `context` key/value pairs directly to the message body as a `Context:` section. They become part of the visible message in the HA sidebar.

```yaml
service: ans.send_notification
data:
  source: "automation.front_door_motion"
  title: "Motion Detected"
  message: "Motion at the front door."
  type: SECURITY
  criticality: HIGH
  context:
    camera: "camera.front_door"
    zone: "entrance"
```

The sidebar notification message will read:

```
Motion at the front door.

Context:
- camera: [camera.front_door](/history?entity_id=camera.front_door)
- zone: entrance
```

Values that match a known HA entity ID are auto-linked to their entity history page.

> **Note:** `context` is only rendered by the persistent notification channel. Signal and Mobile App ignore it.


## Actionable Notifications (Mobile App)

ANS supports HA Companion App action buttons via the top-level `actions` field. The send side goes through ANS; the response side uses a standard HA automation listening for the `mobile_app_notification_action` event — exactly as it would with a direct `notify.mobile_app_*` call.

### Primary Example: Garage Door — Tap to Close

**Automation 1 — Send the actionable notification:**

```yaml
- alias: "Garage door open — actionable notification"
  trigger:
    - platform: state
      entity_id: cover.garage_door
      to: "open"
      for: "00:10:00"
  action:
    - service: ans.send_notification
      data:
        source: "automation.garage_door_open"
        title: "Garage Door Open"
        message: "The garage door has been open for 10 minutes. Close it?"
        type: WARNING
        criticality: HIGH
        channel_data:
          tag: "garage_door_open"     # Used for ack tracking AND to clear the notification
        actions:
          - action: "CLOSE_GARAGE"
            title: "Close Garage"
          - action: "DISMISS_GARAGE"
            title: "Dismiss"
```

**Automation 2 — Handle the button tap:**

```yaml
- alias: "Garage door — handle close action"
  trigger:
    - platform: event
      event_type: mobile_app_notification_action
      event_data:
        action: "CLOSE_GARAGE"
  action:
    - service: cover.close_cover
      target:
        entity_id: cover.garage_door
    - service: notify.mobile_app_alice
      data:
        message: "clear_notification"
        data:
          tag: "garage_door_open"
```

> **Note:** The response automation is pure Home Assistant — ANS is only involved in the delivery. The action identifier (`CLOSE_GARAGE`) is a free-form string you define; it must match between the send and response automations.


### Secondary Example: Unknown Person at Door — Unlock or Ignore

```yaml
# Send
- alias: "Unknown person at door — actionable notification"
  trigger:
    - platform: state
      entity_id: binary_sensor.front_door_motion
      to: "on"
  action:
    - service: ans.send_notification
      data:
        source: "automation.front_door_motion"
        title: "Person at Front Door"
        message: "Unrecognised motion detected. Unlock door?"
        type: SECURITY
        criticality: HIGH
        image: "/api/camera_proxy/camera.front_door"
        channel_data:
          tag: "front_door_unknown"
        actions:
          - action: "UNLOCK_FRONT_DOOR"
            title: "Unlock"
            destructive: true
          - action: "IGNORE_DOOR"
            title: "Ignore"

# Respond
- alias: "Front door — handle unlock action"
  trigger:
    - platform: event
      event_type: mobile_app_notification_action
      event_data:
        action: "UNLOCK_FRONT_DOOR"
  action:
    - service: lock.unlock
      target:
        entity_id: lock.front_door
```


## Acknowledgement Tracking

ANS fires `ans_notification_acknowledged` when the user taps an action button or the notification body on a mobile push notification, or dismisses a persistent notification from the HA sidebar. The event payload always includes `notification_id`, `channel_id`, `acknowledged_at`, and `method` (`action_button`, `notification_tap`, or `persistent_notification_dismiss`). Mobile acknowledgements also include `device_name` and optionally `action` (for button taps). The HA event `context.user_id` carries the HA user who performed the interaction when attribution is available.

> **Note:** Acknowledgement eligibility is persisted across HA restarts. A notification delivered before restart can still emit `ans_notification_acknowledged` when the user interacts after restart.

For the architectural details, see [How It Works → Acknowledgement Tracking](how-it-works.md#acknowledgement-tracking) and [Advanced → `ans_acknowledgements.json`](advanced.md#ans_acknowledgementsjson).

### Example 1 — Cancel escalation on acknowledgement

Send an alert; if the user acknowledges within 5 minutes, skip the escalation:

```yaml
- alias: "Back door open — alert with ack check"
  trigger:
    - platform: state
      entity_id: binary_sensor.back_door
      to: "on"
      for: "00:02:00"
  action:
    - action: ans.send_notification
      data:
        source: "automation.back_door_open"
        title: "Back Door Open"
        message: "The back door has been open for 2 minutes."
        type: SECURITY
        criticality: HIGH
      response_variable: ans_result

    # Wait up to 5 minutes for acknowledgement
    - wait_for_trigger:
        - platform: event
          event_type: ans_notification_acknowledged
          event_data:
            notification_id: "{{ ans_result.notification_id }}"
      timeout: "00:05:00"
      continue_on_timeout: true

    # Skip escalation if acknowledged
    - condition: template
      value_template: "{{ wait.trigger is none }}"

    # Escalate — back door still unacknowledged after 5 minutes
    - action: ans.send_notification
      data:
        source: "automation.back_door_open"
        title: "Back Door Still Open"
        message: "Back door has been open for 7 minutes with no response."
        type: SECURITY
        criticality: CRITICAL
```

### Example 2 — Override the mobile tag via channel_data

When you want a stable custom tag for notification grouping or replacement (rather than the default UUID), set `tag` in `channel_data`. The HA Companion App then uses this tag both for grouping/replacement and for ANS acknowledgement tracking:

```yaml
- alias: "HA update notification with custom tag"
  trigger:
    - platform: state
      entity_id: binary_sensor.updater
      to: "on"
  action:
    - action: ans.send_notification
      data:
        source: "automation.ha_update_check"
        title: "Update Available"
        message: "Home Assistant {{ states('sensor.latest_version') }} is ready."
        type: INFO
        criticality: MEDIUM
        channel_data:
          channel: "updates"        # Android notification channel
          importance: low
          tag: "ha_update"          # Replaces any previous "ha_update" notification
```

When a subsequent update notification fires, the Companion App replaces the previous one (same tag). ANS acknowledgement tracking also uses this tag to correlate taps back to the correct `notification_id`.


## Do Not Disturb Patterns

DND is configured per recipient in Step 6 of the recipient wizard. The following examples show the recipient DND settings alongside the service call behavior.

### Pattern 1 — Basic Quiet Hours (REMINDER silenced at midnight)

**Recipient DND config:**
- Enabled: Yes
- Start: `22:00`, End: `06:00`
- Criticality bypass: `CRITICAL`
- Type bypass: `ALERT`

```yaml
# Sent at 23:30 — FILTERED (type=REMINDER is not in bypass list)
service: ans.send_notification
data:
  source: "automation.medication_reminder"
  title: "Medication Reminder"
  message: "Don't forget your evening medication."
  type: REMINDER
  criticality: MEDIUM
```

The notification is silently discarded for that recipient. No delivery, no retry.


### Pattern 2 — Security Always Gets Through

Same DND config as above, but `SECURITY` type is added to the type bypass list:
- Type bypass: `ALERT`, `SECURITY`

```yaml
# Sent at 02:00 — ALLOWED (type=SECURITY is in bypass list)
service: ans.send_notification
data:
  source: "automation.smoke_alarm"
  title: "Smoke Alarm"
  message: "Smoke detected in the kitchen."
  type: SECURITY
  criticality: HIGH
```

The notification bypasses DND and is delivered immediately, even at 2 AM.


### Pattern 3 — CRITICAL Only at Night

A more restrictive DND that only allows the highest-priority alerts:
- Enabled: Yes
- Start: `23:00`, End: `07:00`
- Criticality bypass: `CRITICAL` (only)
- Type bypass: *(none)*

```yaml
# Sent at 01:00 — FILTERED (criticality=HIGH, not CRITICAL)
service: ans.send_notification
data:
  source: "automation.door_open"
  title: "Front Door Open"
  message: "The front door was opened."
  type: SECURITY
  criticality: HIGH

# Sent at 01:00 — ALLOWED (criticality=CRITICAL is in bypass list)
service: ans.send_notification
data:
  source: "alarm_control_panel.home"
  title: "Alarm Triggered"
  message: "Security alarm has been triggered."
  type: SECURITY
  criticality: CRITICAL
```

Only CRITICAL-level notifications break through after 23:00. Everything else waits or is discarded.


## Source Blocking in Practice

`blocked_sources_regex` lets you prevent specific automations or scripts from sending to a recipient, without modifying those automations.

**Use case:** A production recipient should never receive notifications from test automations or development scripts.

**Recipient basic settings:**
- Blocked sources (regex): `^(automation\.test_|script\.dev_)`

```yaml
# Blocked — source matches the pattern
service: ans.send_notification
data:
  source: "automation.test_motion_alert"
  title: "Test Motion"
  message: "This is a test notification."
  type: INFO
  criticality: LOW

# Allowed — source does not match
service: ans.send_notification
data:
  source: "automation.front_door_motion"
  title: "Motion Detected"
  message: "Real motion at the front door."
  type: SECURITY
  criticality: HIGH
```

The first call is silently discarded for that recipient. The second is processed normally.

> **Tip:** Test your regex at [regex101.com](https://regex101.com) (select Python flavor) before entering it in the recipient wizard. ANS uses `re.match()`, which anchors the pattern to the **start of the string** — a bare pattern like `"motion"` will **not** match `"automation.motion_alert"` because the match is attempted at position 0. Use `^automation\.` to match automation sources, or prefix with `.*` (e.g. `.*motion.*`) to match anywhere in the source string. The leading `^` anchor is redundant but harmless since `re.match()` is already start-anchored.


## Reacting to Delivery Outcomes

ANS fires HA bus events at every terminal outcome of the delivery pipeline. Use these events to build escalation logic, alert on failures, or correlate service calls with results.

For full payload schemas, see [Advanced → Delivery Outcome Events — Payload Reference](advanced.md#delivery-outcome-events--payload-reference) and [How It Works → Delivery Outcome Events](how-it-works.md#delivery-outcome-events).

### Example 1 — Capture the notification ID

Use `response_variable` to capture the `notification_id` returned by `ans.send_notification`, then match it against event data in follow-up steps:

```yaml
- alias: "Security alert with outcome tracking"
  trigger:
    - platform: state
      entity_id: binary_sensor.back_door
      to: "on"
  action:
    - action: ans.send_notification
      data:
        message: "Back door opened"
        type: SECURITY
        criticality: HIGH
      response_variable: ans_result
    # ans_result.notification_id is now available for correlation
    - wait_for_trigger:
        - platform: event
          event_type: ans_notification_settled
          event_data:
            notification_id: "{{ ans_result.notification_id }}"
      timeout: "00:05:00"
      continue_on_timeout: true
```

### Example 2 — Escalate if no one received the notification

React to `ans_notification_settled` when `recipients_delivered` is `0` — meaning no channel delivered successfully for any recipient. Filter by `criticality` to avoid escalating low-priority misses:

```yaml
- alias: "ANS — escalate on total delivery failure"
  trigger:
    - platform: event
      event_type: ans_notification_settled
  condition:
    - condition: template
      value_template: >
        {{ trigger.event.data.recipients_delivered == 0
           and trigger.event.data.criticality in ['HIGH', 'CRITICAL'] }}
  action:
    - action: notify.mobile_app_fallback_device
      data:
        message: >
          ANS delivery failure: notification {{ trigger.event.data.notification_id }}
          reached 0 of {{ trigger.event.data.total_recipients }} recipient(s).
        title: "Notification delivery failed"
```

### Example 3 — Alert on permanent channel failure

Subscribe to `ans_notification_failed` for targeted alerts when a specific channel permanently fails:

```yaml
- alias: "ANS — alert on Signal delivery failure"
  trigger:
    - platform: event
      event_type: ans_notification_failed
      event_data:
        channel_id: "notify.signal"
  action:
    - action: notify.persistent_notification
      data:
        message: >
          Signal delivery failed for notification {{ trigger.event.data.notification_id }}
          (recipient: {{ trigger.event.data.recipient_id }},
          criticality: {{ trigger.event.data.criticality }}).
          Error: {{ trigger.event.data.error }}
        title: "ANS — Signal channel failure"
```

---

## Maintaining Your Setup

### When to Refresh Channels

ANS auto-detects channel changes in real time. You should only need to manually refresh if:

- You installed the HA Companion App on a new phone but `notify.mobile_app_*` hasn't appeared in ANS yet
- You added a new media player entity that should be available as a TTS channel
- You enabled a new notify integration (e.g., Signal) and it's not showing up

**Manual refresh:**

```yaml
service: ans.refresh_channels
```

No parameters required. The refresh completes within a few seconds and takes effect immediately.

**Optional: call refresh on HA startup**

```yaml
- alias: "ANS — refresh channels on startup"
  trigger:
    - platform: homeassistant
      event: start
  action:
    - delay: "00:00:30"   # Brief delay to let all integrations finish loading
    - service: ans.refresh_channels
```

This ensures channels added during the HA startup sequence (e.g., a slow-loading media player integration) are always picked up.

---

*Previous: [← Channel Reference](channels.md) | Next: [Troubleshooting →](troubleshooting.md)*
