# tests/test_filter_engine.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

import pytest
from filter_engine import FilterEngine, FilterResult

from ..models import Identity, IdentityConfig

# -- Helper lightweight model stubs used by the tests --------------------------------


@dataclass
class NotificationEnvelope:
    notification_id: str
    timestamp: datetime | None = None
    source: str | None = None
    title: str | None = None
    body: str | None = None
    type: object | None = None  # can be string or Enum-like with .value
    criticality: str | None = None
    metadata: dict | None = None
    target_identities: list | None = None


# -------------------------------------------------------------------------------------


@pytest.fixture
def engine():
    return FilterEngine()


def make_envelope(source: str | None, type_val: object | None):
    return NotificationEnvelope(
        notification_id="test-1",
        source=source,
        type=type_val,
    )


def make_identity_cfg(
    types=None,
    blocked=None,
    dnd_enabled=False,
    dnd_start=None,
    dnd_end=None,
    dnd_allowed=None,
):
    return IdentityConfig(
        identity_id="alice",
        notification_types=types
        if types is not None
        else ["INFO", "WARNING", "ALERT", "REMINDER", "EVENT", "SECURITY"],
        blocked_sources_pattern=blocked,
        dnd_enabled=dnd_enabled,
        dnd_start=dnd_start,
        dnd_end=dnd_end,
        dnd_allowed_sources_pattern=dnd_allowed,
    )


# -----------------------
# Tests
# -----------------------


def test_allowed_notification_type_passes(engine):
    """If the envelope type is in identity_config.notification_types it should pass."""
    cfg = make_identity_cfg(types=["INFO", "ALERT"])
    env = make_envelope(source="sensor.frontdoor", type_val="INFO")

    res: FilterResult = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert isinstance(res, FilterResult)
    assert res.allowed is True


def test_missing_notification_type_rejected(engine):
    """If envelope.type is None -> reject with missing_notification_type reason."""
    cfg = make_identity_cfg(types=["INFO"])
    env = make_envelope(source="sensor.frontdoor", type_val=None)

    res = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res.allowed is False
    assert res.reason == "missing_notification_type"


def test_blocked_source_pattern_blocks(engine):
    """If blocked_sources_pattern matches the envelope.source the filter rejects."""
    cfg = make_identity_cfg(blocked=r"frontdoor")
    env = make_envelope(source="sensor.frontdoor_v1", type_val="INFO")

    res = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res.allowed is False
    assert res.reason == "blocked_source_pattern"


def test_invalid_blocked_regex_is_skipped(engine):
    """Invalid regex in blocked_sources_pattern should be skipped (allow through and not raise)."""
    # deliberate invalid regex
    cfg = make_identity_cfg(blocked="(")
    env = make_envelope(source="sensor.any", type_val="INFO")

    # Should NOT raise, and should allow (spec: skip invalid regex)
    res = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res.allowed is True


def test_dnd_non_wrap_rejects_when_in_window(engine):
    """DND window not wrapping midnight: in-window rejects, out-of-window allows."""
    # DND from 10:00 to 14:00
    cfg = make_identity_cfg(dnd_enabled=True, dnd_start="10:00:00", dnd_end="14:00:00")
    env = make_envelope(source="sensor.front", type_val="INFO")

    # inside window (11:00)
    res_in = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 11, 0, 0))
    assert res_in.allowed is False
    assert res_in.reason == "dnd_active"

    # outside window (09:00)
    res_out = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 9, 0, 0))
    assert res_out.allowed is True


def test_dnd_wrap_midnight_rejects(engine):
    """DND window wraps midnight (e.g., 22:00 -> 06:00): times during wrap are considered in-DND."""
    cfg = make_identity_cfg(dnd_enabled=True, dnd_start="22:00:00", dnd_end="06:00:00")
    env = make_envelope(source="sensor.x", type_val="INFO")

    # 23:00 => in DND
    res_2300 = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 23, 0, 0))
    assert res_2300.allowed is False

    # 05:00 => in DND (wrap-around early morning)
    res_0500 = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 5, 0, 0))
    assert res_0500.allowed is False

    # 12:00 => not in DND
    res_noon = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res_noon.allowed is True


def test_dnd_with_allowed_exception_pattern_allows(engine):
    """When DND is active but dnd_allowed_sources_pattern matches the source, allow notification."""
    # DND active over a wrap midnight window
    cfg = make_identity_cfg(
        dnd_enabled=True,
        dnd_start="22:00:00",
        dnd_end="06:00:00",
        dnd_allowed=r"^trusted_device",
    )
    # source that matches the allowed-exception pattern
    env = make_envelope(source="trusted_device_door", type_val="INFO")

    # time inside DND
    res = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 23, 30, 0))
    assert res.allowed is True


def test_dnd_allowed_exception_invalid_regex_treated_as_not_allowed(engine):
    """If dnd_allowed_sources_pattern is invalid regex, treat as not allowed (conservative)."""
    cfg = make_identity_cfg(
        dnd_enabled=True,
        dnd_start="22:00:00",
        dnd_end="06:00:00",
        dnd_allowed="(",  # invalid
    )
    env = make_envelope(source="trusted_device_door", type_val="INFO")
    # inside DND time
    res = engine.evaluate(cfg, env, now_dt=datetime(2025, 1, 1, 23, 30, 0))
    assert res.allowed is False
    assert res.reason == "dnd_active"


# Optional: test that notification_types can be enums-like (object with .value)
class FakeEnum:
    def __init__(self, v):
        self.value = v


def test_notification_types_with_enum_like_objects(engine):
    cfg = make_identity_cfg(types=[FakeEnum("INFO"), FakeEnum("ALERT")])
    env_ok = make_envelope(source="s1", type_val=FakeEnum("INFO"))
    env_bad = make_envelope(source="s1", type_val=FakeEnum("REMINDER"))

    res_ok = engine.evaluate(cfg, env_ok, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res_ok.allowed is True

    res_bad = engine.evaluate(cfg, env_bad, now_dt=datetime(2025, 1, 1, 12, 0, 0))
    assert res_bad.allowed is False
    assert res_bad.reason == "notification_type_blocked"
