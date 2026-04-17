"""Unit tests for the ANS diagnostics module.

Coverage targets
----------------
async_get_config_entry_diagnostics
  - happy path: all components present
  - config_repo absent → error key, warning logged
  - channel_manager absent → channels.error key
  - system_config absent → no system_config key in output
  - recipient type counting with Counter → correct totals and type breakdown
  - channel scope counts via Counter → correct by_scope totals
  - channel records serialised correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ..diagnostics import async_get_config_entry_diagnostics
from ..models import ChannelScope, RecipientType

# ---------------------------------------------------------------------------
# Patch target
# ---------------------------------------------------------------------------

# 'get_config_repository' is imported lazily inside the function body via
# 'from . import get_config_repository', so it is resolved from the parent
# package namespace at call time — patch it there.
_PATCH_GET_REPO = "ans.get_config_repository"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(entry_id: str = "test-entry-id", version: int = 1) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.version = version
    return entry


def _make_hass() -> MagicMock:
    return MagicMock()


def _make_channel_info(scope: ChannelScope) -> MagicMock:
    info = MagicMock()
    info.scope = scope
    return info


def _make_channel_record(
    channel_id: str,
    label: str,
    scope: ChannelScope,
    integration: str | None,
    status_value: str,
    has_adapter: bool,
) -> MagicMock:
    rec = MagicMock()
    rec.info = MagicMock()
    rec.info.id = channel_id
    rec.info.label = label
    rec.info.scope = scope
    rec.info.integration = integration
    rec.status = MagicMock()
    rec.status.value = status_value
    rec.adapter = MagicMock() if has_adapter else None
    return rec


def _make_recipient(rtype: RecipientType) -> MagicMock:
    r = MagicMock()
    r.type = rtype
    return r


def _make_channel_manager(
    infos: list[MagicMock] | None = None,
    records: list[MagicMock] | None = None,
    detected: int = 2,
    active: int = 1,
) -> MagicMock:
    mgr = MagicMock()
    mgr.get_all_infos = MagicMock(return_value=infos or [])
    mgr.get_all_records = MagicMock(return_value=records or [])
    mgr.count_detected = MagicMock(return_value=detected)
    mgr.count_active = MagicMock(return_value=active)
    return mgr


def _make_config_repo(
    *,
    channel_manager: MagicMock | None = None,
    system_config: MagicMock | None = None,
    recipients: dict | None = None,
) -> MagicMock:
    repo = MagicMock()
    repo.channel_manager = channel_manager
    repo.system_config = system_config
    repo.recipients = recipients if recipients is not None else {}
    return repo


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_returns_entry_id_and_version(self):
        hass = _make_hass()
        entry = _make_entry(entry_id="abc-123", version=2)
        repo = _make_config_repo(channel_manager=_make_channel_manager())

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["entry_id"] == "abc-123"
        assert result["version"] == 2

    async def test_channels_section_present(self):
        hass = _make_hass()
        entry = _make_entry()
        mgr = _make_channel_manager(detected=3, active=2)
        repo = _make_config_repo(channel_manager=mgr)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        ch = result["channels"]
        assert ch["detected"] == 3
        assert ch["active"] == 2

    async def test_channel_records_serialised(self):
        hass = _make_hass()
        entry = _make_entry()
        rec = _make_channel_record(
            channel_id="notify.mobile_app_phone",
            label="Phone",
            scope=ChannelScope.RECIPIENT,
            integration="mobile_app",
            status_value="active",
            has_adapter=True,
        )
        mgr = _make_channel_manager(records=[rec])
        repo = _make_config_repo(channel_manager=mgr)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        records = result["channels"]["records"]
        assert len(records) == 1
        r = records[0]
        assert r["id"] == "notify.mobile_app_phone"
        assert r["label"] == "Phone"
        assert r["scope"] == ChannelScope.RECIPIENT.value
        assert r["integration"] == "mobile_app"
        assert r["status"] == "active"
        assert r["has_adapter"] is True

    async def test_channel_record_no_adapter(self):
        hass = _make_hass()
        entry = _make_entry()
        rec = _make_channel_record(
            channel_id="notify.persistent_notification",
            label="PN",
            scope=ChannelScope.SYSTEM,
            integration=None,
            status_value="active",
            has_adapter=False,
        )
        mgr = _make_channel_manager(records=[rec])
        repo = _make_config_repo(channel_manager=mgr)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["channels"]["records"][0]["has_adapter"] is False

    async def test_system_config_section_present(self):
        hass = _make_hass()
        entry = _make_entry()
        sys_cfg = MagicMock()
        sys_cfg.enabled_channels = ["notify.persistent_notification"]
        repo = _make_config_repo(
            channel_manager=_make_channel_manager(), system_config=sys_cfg
        )

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["system_config"]["enabled_channels"] == [
            "notify.persistent_notification"
        ]

    async def test_recipient_totals_and_type_counts(self):
        hass = _make_hass()
        entry = _make_entry()
        recipients = {
            "r1": _make_recipient(RecipientType.HA_USER),
            "r2": _make_recipient(RecipientType.HA_USER),
            "r3": _make_recipient(RecipientType.GENERIC),
            "r4": _make_recipient(RecipientType.TTS),
        }
        repo = _make_config_repo(
            channel_manager=_make_channel_manager(), recipients=recipients
        )

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        rcpts = result["recipients"]
        assert rcpts["total"] == 4
        assert rcpts["types"][RecipientType.HA_USER.value] == 2
        assert rcpts["types"][RecipientType.GENERIC.value] == 1
        assert rcpts["types"][RecipientType.TTS.value] == 1

    async def test_no_recipients_returns_empty_types(self):
        hass = _make_hass()
        entry = _make_entry()
        repo = _make_config_repo(channel_manager=_make_channel_manager(), recipients={})

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["recipients"]["total"] == 0
        assert result["recipients"]["types"] == {}


# ---------------------------------------------------------------------------
# Channel scope counts via Counter
# ---------------------------------------------------------------------------


class TestChannelScopeCounts:
    async def test_by_scope_counts_all_three_scopes(self):
        hass = _make_hass()
        entry = _make_entry()
        infos = [
            _make_channel_info(ChannelScope.SYSTEM),
            _make_channel_info(ChannelScope.SYSTEM),
            _make_channel_info(ChannelScope.RECIPIENT),
            _make_channel_info(ChannelScope.TTS),
            _make_channel_info(ChannelScope.TTS),
            _make_channel_info(ChannelScope.TTS),
        ]
        mgr = _make_channel_manager(infos=infos)
        repo = _make_config_repo(channel_manager=mgr)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        by_scope = result["channels"]["by_scope"]
        assert by_scope["system"] == 2
        assert by_scope["recipient"] == 1
        assert by_scope["tts"] == 3

    async def test_by_scope_zero_when_no_channels(self):
        hass = _make_hass()
        entry = _make_entry()
        mgr = _make_channel_manager(infos=[])
        repo = _make_config_repo(channel_manager=mgr)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        by_scope = result["channels"]["by_scope"]
        assert by_scope["system"] == 0
        assert by_scope["recipient"] == 0
        assert by_scope["tts"] == 0


# ---------------------------------------------------------------------------
# Error / absent-component paths
# ---------------------------------------------------------------------------


class TestAbsentComponents:
    async def test_no_config_repo_returns_error_key(self):
        hass = _make_hass()
        entry = _make_entry()

        with patch(_PATCH_GET_REPO, return_value=None):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert "error" in result
        assert result["error"] == "Config repository not initialized"

    async def test_no_config_repo_logs_warning(self, caplog):
        import logging

        hass = _make_hass()
        entry = _make_entry(entry_id="warn-entry")

        with (
            patch(_PATCH_GET_REPO, return_value=None),
            caplog.at_level(
                logging.WARNING, logger="custom_components.ans.diagnostics"
            ),
        ):
            await async_get_config_entry_diagnostics(hass, entry)

        assert "warn-entry" in caplog.text

    async def test_no_config_repo_does_not_include_channels_or_recipients(self):
        hass = _make_hass()
        entry = _make_entry()

        with patch(_PATCH_GET_REPO, return_value=None):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert "channels" not in result
        assert "recipients" not in result

    async def test_no_channel_manager_returns_channels_error_key(self):
        hass = _make_hass()
        entry = _make_entry()
        repo = _make_config_repo(channel_manager=None)

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert "error" in result["channels"]

    async def test_no_system_config_omits_system_config_key(self):
        hass = _make_hass()
        entry = _make_entry()
        repo = _make_config_repo(
            channel_manager=_make_channel_manager(), system_config=None
        )

        with patch(_PATCH_GET_REPO, return_value=repo):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert "system_config" not in result
