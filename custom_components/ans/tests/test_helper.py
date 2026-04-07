"""Unit tests for custom_components.ans.helper.

Coverage targets
----------------
- get_main_entry            — happy path, unique_id fallback, no entries
- get_subentries            — happy path, missing main entry
- check_recipient_name_availability
                            — available name, taken name, missing main entry,
                              subentry with missing name key
- get_not_configured_ha_users
                            — happy path (some configured), all unconfigured,
                              no main entry, auth failure, mixed missing id keys
- calculate_suggested_rate_limit
                            — typical values, boundary clamp, invalid input
- dict_to_select_options    — populated dict, empty dict, None labels
- channel_info_to_select_options
                            — populated list, empty list
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ..exceptions import ConfigEntryNotFoundError
from ..helper import (
    calculate_suggested_rate_limit,
    channel_info_to_select_options,
    check_recipient_name_availability,
    dict_to_select_options,
    get_main_entry,
    get_not_configured_ha_users,
    get_subentries,
)
from ..models import ChannelInfo, ChannelScope

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_hass(entries: list | None = None) -> MagicMock:
    """Return a minimal HomeAssistant mock with config_entries pre-wired."""
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = entries or []
    return hass


def _make_entry(
    unique_id: str | None = None, subentries: dict | None = None
) -> MagicMock:
    """Return a mock ConfigEntry."""
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.subentries = subentries or {}
    return entry


def _make_subentry(subentry_id: str = "sub1", data: dict | None = None) -> MagicMock:
    """Return a mock ConfigSubentry."""
    sub = MagicMock()
    sub.subentry_id = subentry_id
    sub.data = data if data is not None else {"id": "user_1", "name": "Alice"}
    return sub


def _make_ha_user(uid: str, name: str | None = "Alice") -> MagicMock:
    """Return a mock HA User object."""
    user = MagicMock()
    user.id = uid
    user.name = name
    return user


# ---------------------------------------------------------------------------
# get_main_entry
# ---------------------------------------------------------------------------


class TestGetMainEntry:
    def test_returns_entry_matching_domain_unique_id(self):
        """Entry with unique_id == DOMAIN is preferred over others."""
        from ..const import DOMAIN

        other = _make_entry(unique_id="other")
        main = _make_entry(unique_id=DOMAIN)
        hass = _make_hass(entries=[other, main])

        result = get_main_entry(hass)

        assert result is main

    def test_fallback_to_first_entry_when_no_domain_unique_id(self):
        """Falls back to first entry when none has unique_id == DOMAIN."""
        first = _make_entry(unique_id="something-else")
        second = _make_entry(unique_id="yet-another")
        hass = _make_hass(entries=[first, second])

        result = get_main_entry(hass)

        assert result is first

    def test_returns_none_when_no_entries(self):
        hass = _make_hass(entries=[])

        assert get_main_entry(hass) is None

    def test_returns_none_when_entries_is_empty_list(self):
        hass = _make_hass(entries=[])

        assert get_main_entry(hass) is None

    def test_single_entry_without_matching_unique_id_still_returned(self):
        entry = _make_entry(unique_id="unrelated")
        hass = _make_hass(entries=[entry])

        result = get_main_entry(hass)

        assert result is entry


# ---------------------------------------------------------------------------
# get_subentries
# ---------------------------------------------------------------------------


class TestGetSubentries:
    def test_returns_list_of_subentries(self):
        from ..const import DOMAIN

        sub1 = _make_subentry("s1")
        sub2 = _make_subentry("s2")
        main = _make_entry(unique_id=DOMAIN, subentries={"s1": sub1, "s2": sub2})
        hass = _make_hass(entries=[main])

        result = get_subentries(hass)

        assert set(result) == {sub1, sub2}

    def test_returns_empty_list_when_no_subentries(self):
        from ..const import DOMAIN

        main = _make_entry(unique_id=DOMAIN, subentries={})
        hass = _make_hass(entries=[main])

        assert get_subentries(hass) == []

    def test_raises_when_no_main_entry(self):
        hass = _make_hass(entries=[])

        with pytest.raises(ConfigEntryNotFoundError):
            get_subentries(hass)


# ---------------------------------------------------------------------------
# check_recipient_name_availability
# ---------------------------------------------------------------------------


class TestCheckRecipientNameAvailability:
    def _hass_with_subentries(self, *subentries) -> MagicMock:
        from ..const import DOMAIN

        main = _make_entry(
            unique_id=DOMAIN,
            subentries={s.subentry_id: s for s in subentries},
        )
        return _make_hass(entries=[main])

    def test_name_is_available_when_no_subentries(self):
        hass = self._hass_with_subentries()

        assert check_recipient_name_availability(hass, "Alice") is True

    def test_name_is_available_when_different_names_exist(self):
        sub = _make_subentry(data={"name": "Bob"})
        hass = self._hass_with_subentries(sub)

        assert check_recipient_name_availability(hass, "Alice") is True

    def test_name_is_taken_when_exact_match_exists(self):
        sub = _make_subentry(data={"name": "Alice"})
        hass = self._hass_with_subentries(sub)

        assert check_recipient_name_availability(hass, "Alice") is False

    def test_name_check_is_case_sensitive(self):
        sub = _make_subentry(data={"name": "alice"})
        hass = self._hass_with_subentries(sub)

        # "Alice" ≠ "alice" — should be available
        assert check_recipient_name_availability(hass, "Alice") is True

    def test_raises_when_no_main_entry(self):
        hass = _make_hass(entries=[])

        with pytest.raises(ConfigEntryNotFoundError):
            check_recipient_name_availability(hass, "Alice")

    def test_subentry_missing_name_key_does_not_raise(self):
        """A malformed subentry without 'name' in data must not crash."""
        sub = _make_subentry(data={})  # no "name" key
        hass = self._hass_with_subentries(sub)

        # name key missing → data.get("name") is None → "Alice" != None → available
        assert check_recipient_name_availability(hass, "Alice") is True

    def test_multiple_subentries_one_taken(self):
        sub1 = _make_subentry("s1", data={"name": "Bob"})
        sub2 = _make_subentry("s2", data={"name": "Carol"})
        hass = self._hass_with_subentries(sub1, sub2)

        assert check_recipient_name_availability(hass, "Carol") is False
        assert check_recipient_name_availability(hass, "Dave") is True


# ---------------------------------------------------------------------------
# get_not_configured_ha_users
# ---------------------------------------------------------------------------


class TestGetNotConfiguredHaUsers:
    async def test_returns_unconfigured_users_only(self):
        """Users referenced by a subentry are excluded from the result."""
        from ..const import DOMAIN

        sub = _make_subentry(data={"id": "uid_alice", "name": "Alice"})
        main = _make_entry(unique_id=DOMAIN, subentries={"s1": sub})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(
            return_value=[
                _make_ha_user("uid_alice", "Alice"),
                _make_ha_user("uid_bob", "Bob"),
            ]
        )

        result = await get_not_configured_ha_users(hass)

        assert result == {"uid_bob": "Bob"}

    async def test_all_users_unconfigured_when_no_subentries(self):
        from ..const import DOMAIN

        main = _make_entry(unique_id=DOMAIN, subentries={})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(
            return_value=[
                _make_ha_user("uid_alice", "Alice"),
                _make_ha_user("uid_bob", "Bob"),
            ]
        )

        result = await get_not_configured_ha_users(hass)

        assert result == {"uid_alice": "Alice", "uid_bob": "Bob"}

    async def test_empty_result_when_all_users_configured(self):
        from ..const import DOMAIN

        sub = _make_subentry(data={"id": "uid_alice", "name": "Alice"})
        main = _make_entry(unique_id=DOMAIN, subentries={"s1": sub})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(
            return_value=[_make_ha_user("uid_alice", "Alice")]
        )

        result = await get_not_configured_ha_users(hass)

        assert result == {}

    async def test_returns_all_users_when_no_main_entry(self):
        """With no main entry every user is treated as unconfigured."""
        hass = _make_hass(entries=[])
        hass.auth.async_get_users = AsyncMock(
            return_value=[
                _make_ha_user("uid_alice", "Alice"),
                _make_ha_user("uid_bob", "Bob"),
            ]
        )

        result = await get_not_configured_ha_users(hass)

        assert result == {"uid_alice": "Alice", "uid_bob": "Bob"}

    async def test_returns_empty_dict_when_auth_raises(self):
        """auth.async_get_users() failure must return {} without re-raising."""
        hass = _make_hass(entries=[])
        hass.auth.async_get_users = AsyncMock(side_effect=RuntimeError("auth broken"))

        result = await get_not_configured_ha_users(hass)

        assert result == {}

    async def test_handles_user_with_none_name(self):
        """HA users may have name=None (system accounts)."""
        from ..const import DOMAIN

        main = _make_entry(unique_id=DOMAIN, subentries={})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(
            return_value=[_make_ha_user("uid_sys", None)]
        )

        result = await get_not_configured_ha_users(hass)

        assert result == {"uid_sys": None}

    async def test_subentry_missing_id_key_does_not_match_user(self):
        """Malformed subentry without 'id' key must not accidentally exclude users."""
        from ..const import DOMAIN

        sub = _make_subentry(data={"name": "Alice"})  # no "id" key
        main = _make_entry(unique_id=DOMAIN, subentries={"s1": sub})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(
            return_value=[_make_ha_user("uid_alice", "Alice")]
        )

        result = await get_not_configured_ha_users(hass)

        # uid_alice was not matched (subentry had no 'id') → appears as unconfigured
        assert "uid_alice" in result

    async def test_returns_empty_dict_when_no_users(self):
        """No HA users → empty mapping regardless of config."""
        from ..const import DOMAIN

        main = _make_entry(unique_id=DOMAIN, subentries={})
        hass = _make_hass(entries=[main])
        hass.auth.async_get_users = AsyncMock(return_value=[])

        result = await get_not_configured_ha_users(hass)

        assert result == {}


# ---------------------------------------------------------------------------
# calculate_suggested_rate_limit
# ---------------------------------------------------------------------------


class TestCalculateSuggestedRateLimit:
    def test_typical_value(self):
        assert calculate_suggested_rate_limit(100) == 20

    def test_small_value_floored_to_one(self):
        # 20% of 4 = 0.8 → int → 0 → max(1, 0) = 1
        assert calculate_suggested_rate_limit(4) == 1

    def test_value_of_one(self):
        # 20% of 1 = 0.2 → 0 → max(1, 0) = 1
        assert calculate_suggested_rate_limit(1) == 1

    def test_value_capped_at_max(self):
        from ..const import RCPT_MAX_RATE_LIMIT

        # Very large global limit should not exceed RCPT_MAX_RATE_LIMIT
        very_large = RCPT_MAX_RATE_LIMIT * 100
        result = calculate_suggested_rate_limit(very_large)
        assert result == RCPT_MAX_RATE_LIMIT

    def test_value_exactly_at_cap_boundary(self):
        from ..const import RCPT_MAX_RATE_LIMIT

        # global_limit where 20% exactly equals RCPT_MAX_RATE_LIMIT
        global_limit = RCPT_MAX_RATE_LIMIT * 5
        assert calculate_suggested_rate_limit(global_limit) == RCPT_MAX_RATE_LIMIT

    def test_raises_on_zero(self):
        with pytest.raises(ValueError, match="positive integer"):
            calculate_suggested_rate_limit(0)

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="positive integer"):
            calculate_suggested_rate_limit(-10)

    def test_proportional_for_mid_range(self):
        # 20% of 50 = 10
        assert calculate_suggested_rate_limit(50) == 10

    def test_result_always_at_least_one(self):
        for limit in [1, 2, 3, 4, 5]:
            assert calculate_suggested_rate_limit(limit) >= 1


# ---------------------------------------------------------------------------
# dict_to_select_options
# ---------------------------------------------------------------------------


class TestDictToSelectOptions:
    def test_converts_dict_to_select_options(self):
        data = {"val1": "Label One", "val2": "Label Two"}

        result = dict_to_select_options(data)

        assert len(result) == 2
        values = {opt["value"] for opt in result}
        labels = {opt["label"] for opt in result}
        assert values == {"val1", "val2"}
        assert labels == {"Label One", "Label Two"}

    def test_empty_dict_returns_empty_list(self):
        assert dict_to_select_options({}) == []

    def test_none_label_becomes_empty_string(self):
        data = {"key1": None}

        result = dict_to_select_options(data)  # type: ignore[arg-type]

        assert result[0]["label"] == ""
        assert result[0]["value"] == "key1"

    def test_preserves_insertion_order(self):
        data = {"a": "Alpha", "b": "Beta", "c": "Gamma"}

        result = dict_to_select_options(data)

        assert [opt["value"] for opt in result] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# channel_info_to_select_options
# ---------------------------------------------------------------------------


class TestChannelInfoToSelectOptions:
    def _make_channel(self, cid: str, label: str) -> ChannelInfo:
        return ChannelInfo(
            id=cid,
            label=label,
            scope=ChannelScope.SYSTEM,
        )

    def test_converts_channels_to_select_options(self):
        channels = [
            self._make_channel("notify.mobile_app", "Mobile App"),
            self._make_channel("notify.persistent_notification", "Persistent"),
        ]

        result = channel_info_to_select_options(channels)

        assert len(result) == 2
        values = {opt["value"] for opt in result}
        labels = {opt["label"] for opt in result}
        assert values == {"notify.mobile_app", "notify.persistent_notification"}
        assert labels == {"Mobile App", "Persistent"}

    def test_empty_list_returns_empty_list(self):
        assert channel_info_to_select_options([]) == []

    def test_preserves_channel_order(self):
        channels = [
            self._make_channel("ch_a", "A"),
            self._make_channel("ch_b", "B"),
            self._make_channel("ch_c", "C"),
        ]

        result = channel_info_to_select_options(channels)

        assert [opt["value"] for opt in result] == ["ch_a", "ch_b", "ch_c"]

    def test_uses_channel_id_as_value_and_label_as_label(self):
        channels = [self._make_channel("my_id", "My Label")]

        result = channel_info_to_select_options(channels)

        assert result[0]["value"] == "my_id"
        assert result[0]["label"] == "My Label"
