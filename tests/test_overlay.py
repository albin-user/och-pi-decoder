"""Tests for overlay formatting."""

import pytest
from datetime import datetime, timezone, timedelta

from pi_decoder.overlay import (
    format_countdown,
    _ass_alpha,
    _format_schedule_status,
    format_overlay,
)
from pi_decoder.config import OverlayConfig
from pi_decoder.pco_client import LiveStatus

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


class TestFormatCountdown:
    """Test countdown timer formatting."""

    def test_format_seconds_only(self):
        assert format_countdown(45) == "00:45"

    def test_format_minutes_seconds(self):
        assert format_countdown(125) == "02:05"
        assert format_countdown(600) == "10:00"

    def test_format_hours_minutes_seconds(self):
        assert format_countdown(3661) == "1:01:01"
        assert format_countdown(7200) == "2:00:00"

    def test_format_negative(self):
        assert format_countdown(-30) == "-00:30"
        assert format_countdown(-125) == "-02:05"
        assert format_countdown(-3661) == "-1:01:01"

    def test_format_zero(self):
        assert format_countdown(0) == "00:00"


class TestAssAlpha:
    """Test ASS alpha value conversion."""

    def test_fully_opaque(self):
        # transparency 1.0 = fully visible = alpha 00
        assert _ass_alpha(1.0) == "&H00"

    def test_fully_transparent(self):
        # transparency 0.0 = invisible = alpha FF
        assert _ass_alpha(0.0) == "&HFF"

    def test_half_transparent(self):
        # transparency 0.5 = half visible = alpha ~7F
        result = _ass_alpha(0.5)
        assert result in ("&H7F", "&H80")  # allow for rounding

    def test_common_value(self):
        # transparency 0.7 = mostly visible
        result = _ass_alpha(0.7)
        # (1.0 - 0.7) * 255 = 0.3 * 255 = 76.5 -> 76 = 0x4C
        assert result == "&H4C"


class TestFormatScheduleStatus:
    """Test schedule status formatting."""

    def test_overtime(self):
        result = _format_schedule_status(
            remaining=-30,
            planned_service_end=None,
            now=datetime.now(timezone.utc),
            local_tz=ZoneInfo("UTC"),
        )
        assert result == "OVERTIME"

    def test_no_planned_end(self):
        now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        result = _format_schedule_status(
            remaining=1800,  # 30 minutes
            planned_service_end=None,
            now=now,
            local_tz=ZoneInfo("UTC"),
        )
        assert result == "Ends at 10:30"

    def test_on_time(self):
        now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        planned_end = now + timedelta(seconds=1800)
        result = _format_schedule_status(
            remaining=1800,
            planned_service_end=planned_end,
            now=now,
            local_tz=ZoneInfo("UTC"),
        )
        assert "on time" in result

    def test_ahead_of_schedule(self):
        now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        planned_end = now + timedelta(minutes=35)
        result = _format_schedule_status(
            remaining=1500,  # 25 min remaining, planned was 35
            planned_service_end=planned_end,
            now=now,
            local_tz=ZoneInfo("UTC"),
        )
        assert "ahead" in result

    def test_behind_schedule(self):
        now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        planned_end = now + timedelta(minutes=20)
        result = _format_schedule_status(
            remaining=2100,  # 35 min remaining, planned was 20
            planned_service_end=planned_end,
            now=now,
            local_tz=ZoneInfo("UTC"),
        )
        assert "behind" in result


class TestFormatOverlay:
    """Test full overlay formatting."""

    @pytest.fixture
    def default_config(self):
        return OverlayConfig()

    def test_not_live_shows_message(self, default_config):
        status = LiveStatus(is_live=False, message="Waiting...")
        result = format_overlay(status, default_config)
        assert "Waiting..." in result

    def test_not_live_with_plan_title(self, default_config):
        status = LiveStatus(
            is_live=False,
            message="Not live",
            plan_title="Sunday Service",
        )
        result = format_overlay(status, default_config)
        assert "Not live" in result
        assert "Sunday Service" in result

    def test_finished_shows_overtime(self, default_config):
        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            finished=True,
            plan_title="Sunday Service",
            service_end_time=now - timedelta(minutes=5),
        )
        result = format_overlay(status, default_config)
        assert "OVERTIME" in result

    def test_live_shows_countdown(self, default_config):
        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            plan_title="Sunday Service",
            item_title="Worship",
            item_end_time=now + timedelta(minutes=10),
            remaining_items_length=1800,
        )
        result = format_overlay(status, default_config)
        # Should contain formatted time and title
        assert "Sunday Service" in result

    def test_position_tag_applied(self):
        status = LiveStatus(is_live=False, message="Test")

        for pos in ["top-left", "top-right", "bottom-left", "bottom-right"]:
            cfg = OverlayConfig(position=pos)
            result = format_overlay(status, cfg)
            # ASS position tags
            assert "\\an" in result

    def test_font_size_applied(self):
        status = LiveStatus(is_live=False, message="Test")
        cfg = OverlayConfig(font_size=100, font_size_title=50, font_size_info=30)
        result = format_overlay(status, cfg)
        assert "\\fs50" in result  # font_size_title for message

    def test_item_mode_shows_item_info(self):
        now = datetime.now(timezone.utc)
        cfg = OverlayConfig(timer_mode="item", show_description=True)
        status = LiveStatus(
            is_live=True,
            item_title="Sermon",
            item_description="Pastor John",
            item_end_time=now + timedelta(minutes=25),
        )
        result = format_overlay(status, cfg)
        assert "Sermon" in result
        assert "Pastor John" in result
