# filter_engine.py
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterResult:
    """Result of a filter evaluation."""

    allowed: bool
    reason: str | None = None
    matched_exceptions: list[str] | None = None


class FilterEngine:
    """FilterEngine evaluates whether a NotificationEnvelope should be delivered
    to a particular identity according to IdentityConfig rules:
      - allowed notification types
      - blocked source regex (string; may be None/empty)
      - DND mode (enabled/disabled) and DND window (HH:MM:SS) with exceptions

    NOTE: identity and identity_config are required and a ValueError is raised
    if either is not provided.

    """

    # Safety: maximum pattern length to avoid pathological config values.
    MAX_PATTERN_LENGTH = 4096

    def __init__(self) -> None:
        # Simple cache of compiled regex objects keyed by pattern string.
        self._pattern_cache: dict[str, re.Pattern] = {}

    def evaluate(
        self,
        identity_config: object,
        envelope: object,
        *,
        now_dt: datetime | None = None,
    ) -> FilterResult:
        """Evaluate whether `envelope` should be delivered to `identity` given `identity_config`.

        Args:
            identity: Identity object (required)
            identity_config: IdentityConfig object (required)
            envelope: NotificationEnvelope (expected to have .type and .source)
            now_dt: optional datetime to use for "now" (useful for unit tests)

        Returns:
            FilterResult

        Raises:
            ValueError: if identity or identity_config is None

        """
        # Required inputs
        if identity_config is None:
            raise ValueError("identity_config is required for filter evaluation")

        # Determine now using HA timezone-aware helpers if possible, unless overridden by now_dt
        if now_dt is None:
            try:
                # Prefer Home Assistant timezone-aware now
                ha_now = dt_util.now()
                # Convert to local HA time
                local_now = dt_util.as_local(ha_now)
                now_dt = local_now
            except Exception:
                # Fallback to naive local time
                now_dt = datetime.now()

        now_time = now_dt.time()

        # 1) Notification type allowed?
        try:
            allowed_types = getattr(identity_config, "notification_types", None)
            if allowed_types is not None:
                notif_type = getattr(envelope, "type", None)
                if notif_type is None:
                    _LOGGER.debug(
                        "FilterEngine: envelope missing 'type' field; rejecting."
                    )
                    return FilterResult(False, reason="missing_notification_type")

                # Normalize allowed values (support enums or raw strings)
                allowed_values = set()
                for t in allowed_types:
                    allowed_values.add(getattr(t, "value", t))
                notif_value = getattr(notif_type, "value", notif_type)
                if notif_value not in allowed_values:
                    _LOGGER.info(
                        "FilterEngine: notification type '%s' not allowed for identity %s",
                        notif_value,
                        getattr(identity_config, "identity_id", "<unknown>"),
                    )
                    return FilterResult(False, reason="notification_type_blocked")
        except Exception as exc:
            _LOGGER.warning(
                "FilterEngine: error evaluating notification_types: %s", exc
            )
            # Conservative reject on unexpected errors evaluating types
            return FilterResult(False, reason="notification_types_eval_error")

        # 2) Blocked source pattern (optional)
        source = getattr(envelope, "source", None)
        blocked_pattern = getattr(identity_config, "blocked_sources_pattern", None)
        if blocked_pattern:
            try:
                if self._match_pattern(blocked_pattern, source):
                    _LOGGER.info(
                        "FilterEngine: blocked source pattern matched for identity %s (source=%s)",
                        getattr(identity_config, "identity_id", "<unknown>"),
                        source,
                    )
                    return FilterResult(False, reason="blocked_source_pattern")
            except re.error:
                # Invalid regex — per spec: skip with warning (do NOT reject)
                _LOGGER.warning(
                    "FilterEngine: invalid blocked_sources_pattern for identity %s; skipping that pattern",
                    getattr(identity_config, "identity_id", "<unknown>"),
                )
            except Exception as exc:
                _LOGGER.warning(
                    "FilterEngine: unexpected error matching blocked pattern: %s", exc
                )
                # Skip conservative behavior: allow through this filter if unexpected internal error

        # 3) DND handling — only when enabled
        dnd_enabled = bool(getattr(identity_config, "dnd_enabled", False))
        if dnd_enabled:
            dnd_start_str = getattr(identity_config, "dnd_start", None)
            dnd_end_str = getattr(identity_config, "dnd_end", None)

            parsed = self._parse_time_window(dnd_start_str, dnd_end_str)
            if parsed is None:
                _LOGGER.warning(
                    "FilterEngine: DND enabled but start/end invalid for identity %s; skipping DND check",
                    getattr(identity_config, "identity_id", "<unknown>"),
                )
            else:
                start_time, end_time = parsed
                in_dnd = self._is_time_in_window(now_time, start_time, end_time)
                if in_dnd:
                    # DND active — check allowed exceptions pattern (optional)
                    dnd_allowed_pattern = getattr(
                        identity_config, "dnd_allowed_sources_pattern", None
                    )
                    if dnd_allowed_pattern:
                        try:
                            if self._match_pattern(dnd_allowed_pattern, source):
                                # allowed by exception
                                _LOGGER.debug(
                                    "FilterEngine: DND exception matched for identity %s (source=%s)",
                                    getattr(
                                        identity_config, "identity_id", "<unknown>"
                                    ),
                                    source,
                                )
                            else:
                                _LOGGER.info(
                                    "FilterEngine: DND active and source not allowed for identity %s",
                                    getattr(
                                        identity_config, "identity_id", "<unknown>"
                                    ),
                                )
                                return FilterResult(False, reason="dnd_active")
                        except re.error:
                            # Invalid regex for exception -> per spec: skip with warning
                            _LOGGER.warning(
                                "FilterEngine: invalid dnd_allowed_sources_pattern for identity %s; treating as not allowed",
                                getattr(identity_config, "identity_id", "<unknown>"),
                            )
                            return FilterResult(False, reason="dnd_active")
                        except Exception as exc:
                            _LOGGER.warning(
                                "FilterEngine: unexpected error matching DND exception pattern: %s",
                                exc,
                            )
                            return FilterResult(False, reason="dnd_active")
                    else:
                        _LOGGER.info(
                            "FilterEngine: DND active and no allowed-exception pattern for identity %s",
                            getattr(identity_config, "identity_id", "<unknown>"),
                        )
                        return FilterResult(False, reason="dnd_active")

        # All checks passed
        _LOGGER.debug(
            "FilterEngine: notification allowed for identity %s",
            getattr(identity_config, "identity_id", "<unknown>"),
        )
        return FilterResult(True, reason=None)

    # -------------------------
    # Helpers
    # -------------------------
    def _match_pattern(self, pattern: str, text: str | None) -> bool:
        """Compile pattern (cached) and attempt to search in the provided text.

        Raises:
            re.error if the pattern is invalid (caller handles spec-required behavior).

        """
        if not pattern:
            return False
        if len(pattern) > self.MAX_PATTERN_LENGTH:
            _LOGGER.warning(
                "FilterEngine: regex pattern length exceeds maximum (%d)",
                self.MAX_PATTERN_LENGTH,
            )
            return False

        compiled = self._pattern_cache.get(pattern)
        if compiled is None:
            # May raise re.error
            compiled = re.compile(pattern)
            self._pattern_cache[pattern] = compiled

        text_to_match = "" if text is None else str(text)
        return bool(compiled.search(text_to_match))

    def _parse_time_window(
        self, start_str: str | None, end_str: str | None
    ) -> tuple[time, time] | None:
        """Parse DND start/end strings in 'HH:MM:SS' format. Return (start_time, end_time), or None if parsing fails."""
        if not start_str or not end_str:
            _LOGGER.warning("FilterEngine: DND start/end not provided or empty")
            return None
        try:
            start_time = self._parse_time_string(start_str)
            end_time = self._parse_time_string(end_str)
            return start_time, end_time
        except ValueError as exc:
            _LOGGER.warning(
                "FilterEngine: invalid DND time format: %s / %s; error=%s",
                start_str,
                end_str,
                exc,
            )
            return None

    @staticmethod
    def _parse_time_string(ts: str) -> time:
        """Parse 'HH:MM:SS' into datetime.time. Raise ValueError on invalid format."""
        parts = ts.split(":")
        if len(parts) != 3:
            raise ValueError("time string must be in HH:MM:SS format")
        h, m, s = parts
        return time(int(h), int(m), int(s))

    @staticmethod
    def _is_time_in_window(now: time, start: time, end: time) -> bool:
        """Determine whether 'now' is inside the inclusive start / exclusive end window.

        Handles midnight wrap (start > end).
        """
        if start <= end:
            return start <= now < end
        return now >= start or now < end
