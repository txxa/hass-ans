"""Unit tests for the Signal delivery adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from ...channels.signal import SignalDeliveryAdapter
from ...models.delivery import DeliveryStatus
from ...models.notification import (
    NotificationCriticality,
    NotificationPayload,
    NotificationType,
)
from ...models.recipient import RecipientContactInfo


def _make_payload(
    title: str = "",
    message: str = "Hello world",
    channel_data: dict | None = None,
    **kwargs,
) -> NotificationPayload:
    """Build a minimal NotificationPayload for use in signal adapter tests."""
    return NotificationPayload(
        notification_id=str(uuid4()),
        source="test",
        title=title,
        message=message,
        type=NotificationType.INFO,
        criticality=NotificationCriticality.LOW,
        created_at=datetime.now(UTC),
        channel_data=channel_data or {},
        **kwargs,
    )


def _make_contact(phone: str | None = "+49123456789") -> RecipientContactInfo:
    """Build a RecipientContactInfo with an optional phone number."""
    return RecipientContactInfo(email_address=None, phone_number=phone)


def _make_adapter(
    config_dir: str = "/config",
) -> tuple[SignalDeliveryAdapter, MagicMock]:
    """Create a SignalDeliveryAdapter with a fully mocked hass instance.

    Parameters
    ----------
    config_dir:
        Root config directory reported by ``hass.config``.  Defaults to
        ``/config`` which covers all existing tests.  Pass a real ``tmp_path``
        sub-directory in path-guard tests that need actual symlink resolution.

    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config.config_dir = config_dir
    hass.config.path = MagicMock(
        side_effect=lambda *args: config_dir + "/" + "/".join(args)
    )
    adapter = SignalDeliveryAdapter(hass=hass)
    return adapter, hass


# ---------------------------------------------------------------------------
# Title formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_auto_styled_bold():
    """Title present, no explicit text_mode → styled mode with bold title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="Motion Alert", message="Detected in living room")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k1",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    call_kwargs = hass.services.async_call.call_args.kwargs
    service_data = call_kwargs["service_data"]

    assert service_data["message"] == "**Motion Alert**\n\nDetected in living room"
    assert service_data["data"]["text_mode"] == "styled"


@pytest.mark.asyncio
async def test_title_explicit_styled_bold():
    """Title present, explicit text_mode='styled' → bold title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="Alert",
        message="Body text",
        channel_data={"text_mode": "styled"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k2",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "**Alert**\n\nBody text"
    assert service_data["data"]["text_mode"] == "styled"


@pytest.mark.asyncio
async def test_title_explicit_normal_no_bold():
    """Title present but text_mode='normal' overrides auto-styled → plain title."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="Alert",
        message="Body text",
        channel_data={"text_mode": "normal"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k3",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Alert\n\nBody text"
    assert service_data["data"]["text_mode"] == "normal"


@pytest.mark.asyncio
async def test_no_title_no_metadata_normal_mode():
    """No title, no metadata → normal mode, message unchanged."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="", message="Plain message")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k4",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Plain message"
    assert service_data["data"]["text_mode"] == "normal"


@pytest.mark.asyncio
async def test_no_title_with_metadata_normal_mode():
    """No title, explicit text_mode='normal' → plain message."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="Body only",
        channel_data={"text_mode": "normal"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k5",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["message"] == "Body only"
    assert service_data["data"]["text_mode"] == "normal"


# ---------------------------------------------------------------------------
# text_mode validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_text_mode_falls_back_to_normal(caplog):
    """Invalid text_mode value logs a warning and falls back to 'normal'."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="Test",
        channel_data={"text_mode": "fancy"},
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k6",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    service_data = hass.services.async_call.call_args.kwargs["service_data"]
    assert service_data["data"]["text_mode"] == "normal"
    assert "Invalid text_mode" in caplog.text


# ---------------------------------------------------------------------------
# Missing phone number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_phone_permanent_failure():
    """No phone number → permanent failure without calling HA service."""
    adapter, hass = _make_adapter()
    payload = _make_payload(title="Hi", message="There")

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(phone=None),
        idempotency_key="k7",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.PERMANENT_FAIL
    hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Metadata passthrough (attachments, urls, verify_ssl)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachments_and_urls_passed_through():
    """channel_data attachments and urls are forwarded to the service call."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="",
        message="See attached",
        channel_data={
            "text_mode": "normal",
            "attachments": ["/config/www/image.jpg"],
            "urls": ["http://example.com/pic.jpg"],
            "verify_ssl": False,
        },
    )

    result = await adapter.deliver(
        payload=payload,
        contact_info=_make_contact(),
        idempotency_key="k8",
        job_id="job-1",
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert data["attachments"] == ["/config/www/image.jpg"]
    assert data["urls"] == ["http://example.com/pic.jpg"]
    assert data["verify_ssl"] is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_not_found_returns_permanent_failure():
    """ServiceNotFound from the signal service must yield a permanent failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = ServiceNotFound("notify", "signal")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-snf",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL


@pytest.mark.asyncio
async def test_service_validation_error_returns_permanent_failure():
    """ServiceValidationError from the signal service must yield a permanent failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = ServiceValidationError("bad payload")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-sve",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.PERMANENT_FAIL


@pytest.mark.asyncio
async def test_ha_error_returns_transient_failure():
    """Generic HomeAssistantError from the signal service must yield a transient failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = HomeAssistantError("temporary outage")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-hae",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


@pytest.mark.asyncio
async def test_unexpected_exception_returns_transient_failure():
    """An unexpected runtime exception from the signal service yields a transient failure."""
    adapter, hass = _make_adapter()
    hass.services.async_call.side_effect = RuntimeError("unexpected crash")

    result = await adapter.deliver(
        payload=_make_payload(title="Hi", message="Body"),
        contact_info=_make_contact(),
        idempotency_key="k-ux",
        job_id="job-err",
    )
    assert result.status == DeliveryStatus.TRANSIENT_FAIL


# ---------------------------------------------------------------------------
# Class API
# ---------------------------------------------------------------------------


class TestSignalClassAPI:
    """Verify class-level API: channel matching, properties, labels, requirements."""

    def test_matches_channel_returns_true(self):
        """matches_channel returns True for the exact 'notify.signal' channel ID."""
        assert SignalDeliveryAdapter.matches_channel("notify.signal") is True

    def test_matches_channel_returns_false_for_others(self):
        """matches_channel returns False for any other channel ID."""
        assert SignalDeliveryAdapter.matches_channel("notify.mobile_app_x") is False
        assert (
            SignalDeliveryAdapter.matches_channel("notify.persistent_notification")
            is False
        )

    def test_extract_variant_always_none(self):
        """extract_variant always returns None — Signal has no channel variants."""
        assert SignalDeliveryAdapter.extract_variant("notify.signal") is None
        assert SignalDeliveryAdapter.extract_variant("notify.other") is None

    def test_channel_property(self):
        """The channel property returns the canonical 'notify.signal' identifier."""
        adapter, _ = _make_adapter()
        assert adapter.channel == "notify.signal"

    def test_service_name_property(self):
        """service_name strips the 'notify.' prefix from the channel ID."""
        adapter, _ = _make_adapter()
        assert adapter.service_name == "signal"

    def test_get_channel_label(self):
        """get_channel_label returns the human-friendly 'Signal Messenger' string."""
        label = SignalDeliveryAdapter.get_channel_label("notify.signal")
        assert label == "Signal Messenger"

    def test_requires_phone(self):
        """Signal delivery requires a phone number."""
        req = SignalDeliveryAdapter.get_requirements()
        assert req["requires_phone"] is True
        assert req.get("requires_email", False) is False
        assert req.get("requires_ha_user", False) is False


# ---------------------------------------------------------------------------
# _mask_phone helper
# ---------------------------------------------------------------------------


class TestMaskPhone:
    """Unit tests for the _mask_phone module-level helper."""

    def test_mask_phone_shows_last_four(self):
        """Numbers with 4+ digits show the last 4 digits after '****'."""
        from ...channels.signal import _mask_phone  # noqa: PLC0415

        assert _mask_phone("+49123456789") == "****6789"
        assert _mask_phone("1234") == "****1234"

    def test_mask_phone_short_number(self):
        """Numbers shorter than 4 digits return '****' with no digits shown."""
        from ...channels.signal import _mask_phone  # noqa: PLC0415

        assert _mask_phone("123") == "****"
        assert _mask_phone("") == "****"


# ---------------------------------------------------------------------------
# Rich content fields (image, video, file, link)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_url_goes_to_urls():
    """payload.image with http(s) URL is placed in service_data['data']['urls']."""
    adapter, hass = _make_adapter()
    payload = _make_payload(message="Look", image="https://example.com/photo.jpg")

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert "https://example.com/photo.jpg" in data["urls"]


@pytest.mark.asyncio
async def test_image_local_path_goes_to_attachments():
    """payload.image with local path is placed in service_data['data']['attachments']."""
    adapter, hass = _make_adapter(config_dir="/config")
    payload = _make_payload(message="Look", image="/config/www/photo.jpg")

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert "/config/www/photo.jpg" in data["attachments"]


@pytest.mark.asyncio
async def test_video_url_goes_to_urls():
    """payload.video with http(s) URL is placed in service_data['data']['urls']."""
    adapter, hass = _make_adapter()
    payload = _make_payload(message="Clip", video="https://example.com/clip.mp4")

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert "https://example.com/clip.mp4" in data["urls"]


@pytest.mark.asyncio
async def test_file_url_goes_to_urls():
    """payload.file with http(s) URL is placed in service_data['data']['urls']."""
    adapter, hass = _make_adapter()
    payload = _make_payload(message="Doc", file="https://example.com/report.pdf")

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert "https://example.com/report.pdf" in data["urls"]


@pytest.mark.asyncio
async def test_link_appended_to_message_body():
    """payload.link is appended as a plain text line to the message body."""
    adapter, hass = _make_adapter()
    payload = _make_payload(message="See details", link="https://example.com/dash")

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    msg = hass.services.async_call.call_args.kwargs["service_data"]["message"]
    assert "https://example.com/dash" in msg
    assert msg.startswith("See details")


@pytest.mark.asyncio
async def test_link_with_title_appended_after_body():
    """link appears after the message body even when a title is present."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        title="Alert", message="Body text", link="https://example.com"
    )

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    msg = hass.services.async_call.call_args.kwargs["service_data"]["message"]
    # title first, then body+link
    assert msg.index("Body text") < msg.index("https://example.com")


@pytest.mark.asyncio
async def test_multiple_rich_fields_combined():
    """image, file, and link can all be set simultaneously."""
    adapter, hass = _make_adapter()
    payload = _make_payload(
        message="See below",
        image="https://example.com/img.jpg",
        file="https://example.com/doc.pdf",
        link="https://example.com/dash",
    )

    result = await adapter.deliver(
        payload=payload, contact_info=_make_contact(), idempotency_key="k", job_id="j"
    )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
    assert "https://example.com/img.jpg" in data["urls"]
    assert "https://example.com/doc.pdf" in data["urls"]


# ---------------------------------------------------------------------------
# channel_data validation warnings
# ---------------------------------------------------------------------------


class TestSignalChannelDataValidation:
    """Verify that invalid channel_data field types log warnings and are silently dropped."""

    @pytest.mark.asyncio
    async def test_attachments_non_list_logs_warning(self, caplog):
        """Non-list channel_data.attachments logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            channel_data={"attachments": "/single/path.jpg"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-av",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "channel_data.attachments must be a list" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_urls_non_list_logs_warning(self, caplog):
        """Non-list channel_data.urls logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            channel_data={"urls": "http://example.com/img.jpg"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-uv",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "channel_data.urls must be a list" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "urls" not in data

    @pytest.mark.asyncio
    async def test_verify_ssl_non_bool_logs_warning(self, caplog):
        """Non-boolean channel_data.verify_ssl logs a warning and is not forwarded."""
        adapter, hass = _make_adapter()
        payload = _make_payload(
            title="",
            message="Test",
            channel_data={"verify_ssl": "yes"},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-sv",
            job_id="job-val",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "channel_data.verify_ssl must be boolean" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "verify_ssl" not in data


# ---------------------------------------------------------------------------
# Attachment path guard
# ---------------------------------------------------------------------------


class TestSignalAttachmentPathGuard:
    """Verify that _validate_attachment_path rejects paths outside HA-allowed dirs."""

    @pytest.mark.asyncio
    async def test_valid_path_under_config_passes(self):
        """A path inside the config dir is forwarded to the service call."""
        adapter, hass = _make_adapter(config_dir="/config")
        payload = _make_payload(
            title="",
            message="See attached",
            channel_data={"attachments": ["/config/www/snapshot.jpg"]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-ok",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
        assert data["attachments"] == ["/config/www/snapshot.jpg"]

    @pytest.mark.asyncio
    async def test_path_outside_allowed_dirs_dropped_with_warning(self, caplog):
        """A path outside all allowed dirs is dropped and a warning is logged."""
        adapter, hass = _make_adapter(config_dir="/config")
        payload = _make_payload(
            title="",
            message="Sneaky",
            channel_data={"attachments": ["/etc/passwd"]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-out",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "path outside allowed directories" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_traversal_attack_rejected(self, caplog):
        """A path with ../ traversal sequences is rejected after Path.resolve().

        Path.resolve(strict=False) collapses ``..`` sequences without requiring
        the path to exist on the filesystem, so /config/www/../../etc/passwd
        resolves to /etc/passwd and is correctly rejected.
        """
        adapter, hass = _make_adapter(config_dir="/config")
        payload = _make_payload(
            title="",
            message="Traversal",
            channel_data={"attachments": ["/config/www/../../etc/passwd"]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-trav",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "path outside allowed directories" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_symlink_outside_allowed_rejected(self, tmp_path, caplog):
        """A symlink inside the allowed dir that points outside is rejected.

        Path.resolve() follows symlinks, so a symlink inside config/ that points
        to /etc/passwd resolves to /etc/passwd and is rejected.
        """
        allowed_dir = tmp_path / "config"
        allowed_dir.mkdir()
        outside_target = tmp_path / "secret.txt"
        outside_target.write_text("secret")

        symlink = allowed_dir / "evil_link.jpg"
        symlink.symlink_to(outside_target)

        adapter, hass = _make_adapter(config_dir=str(allowed_dir))
        payload = _make_payload(
            title="",
            message="Symlink test",
            channel_data={"attachments": [str(symlink)]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-sym",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "path outside allowed directories" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data

    @pytest.mark.asyncio
    async def test_mixed_attachments_only_valid_forwarded(self, caplog):
        """When some paths are valid and some are not, only valid ones are forwarded."""
        adapter, hass = _make_adapter(config_dir="/config")
        payload = _make_payload(
            title="",
            message="Mixed",
            channel_data={"attachments": ["/config/valid.jpg", "/etc/passwd"]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-mix",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "path outside allowed directories" in caplog.text
        data = hass.services.async_call.call_args.kwargs["service_data"]["data"]
        assert data["attachments"] == ["/config/valid.jpg"]

    @pytest.mark.asyncio
    async def test_all_invalid_attachments_notification_still_delivered(self, caplog):
        """When all attachment paths are invalid the notification is still sent."""
        adapter, hass = _make_adapter(config_dir="/config")
        payload = _make_payload(
            title="",
            message="No attachments",
            channel_data={"attachments": ["/etc/shadow", "/proc/self/environ"]},
        )

        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k-pg-all-inv",
            job_id="job-pg",
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert "path outside allowed directories" in caplog.text
        hass.services.async_call.assert_called_once()
        data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
        assert "attachments" not in data


# ---------------------------------------------------------------------------
# Media URL validation (bare-domain URLs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_domain_image_url_skipped_with_warning(caplog):
    """An image URL with no filename path segment is not added to urls and a warning is logged."""
    import logging

    adapter, hass = _make_adapter()
    payload = _make_payload(message="Look", image="https://example.com")

    with caplog.at_level(
        logging.WARNING, logger="custom_components.ans.channels.signal"
    ):
        result = await adapter.deliver(
            payload=payload,
            contact_info=_make_contact(),
            idempotency_key="k",
            job_id="j",
        )

    assert result.status == DeliveryStatus.SUCCESS
    data = hass.services.async_call.call_args.kwargs["service_data"].get("data", {})
    assert "urls" not in data
    assert any("no filename" in r.message for r in caplog.records)
