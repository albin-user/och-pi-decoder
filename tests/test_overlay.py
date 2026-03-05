"""Tests for overlay formatting and OverlayUpdater."""

import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from pi_decoder.overlay import (
    format_countdown,
    _ass_alpha,
    _format_schedule_status,
    _overlay_layout,
    format_overlay,
    OverlayUpdater,
    OVERLAY_ID,
    OVERLAY_FG_ID,
)
from pi_decoder.config import Config, OverlayConfig
from pi_decoder.mpv_manager import MpvManager
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


class TestOverlayLayout:
    """Test _overlay_layout() dimension calculations."""

    def test_default_1080p(self):
        layout = _overlay_layout(1920, 1080, 96, 38, 32, 1)
        assert layout["edge_x"] == 28  # int(1920 * 0.015)
        assert layout["edge_y"] == 16  # int(1080 * 0.015)
        assert layout["xbord"] > 0
        assert layout["ybord"] > 0
        assert layout["text_margin_x"] == layout["edge_x"] + int(1920 * 0.008)
        assert layout["text_margin_y"] == layout["edge_y"] + int(1080 * 0.008)

    def test_more_info_lines_increases_height(self):
        layout_1 = _overlay_layout(1920, 1080, 96, 38, 32, 1)
        layout_2 = _overlay_layout(1920, 1080, 96, 38, 32, 2)
        assert layout_2["ybord"] > layout_1["ybord"]


class TestFormatOverlay:
    """Test full overlay formatting."""

    @pytest.fixture
    def default_config(self):
        return OverlayConfig()

    def test_not_live_shows_message(self, default_config):
        status = LiveStatus(is_live=False, message="Waiting...")
        _bg, fg = format_overlay(status, default_config)
        assert "Waiting..." in fg

    def test_not_live_with_plan_title(self, default_config):
        status = LiveStatus(
            is_live=False,
            message="Not live",
            plan_title="Sunday Service",
        )
        _bg, fg = format_overlay(status, default_config)
        assert "Not live" in fg
        assert "Sunday Service" in fg

    def test_finished_shows_overtime(self, default_config):
        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            finished=True,
            plan_title="Sunday Service",
            service_end_time=now - timedelta(minutes=5),
        )
        _bg, fg = format_overlay(status, default_config)
        assert "OVERTIME" in fg

    def test_live_shows_countdown(self, default_config):
        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            plan_title="Sunday Service",
            item_title="Worship",
            item_end_time=now + timedelta(minutes=10),
            remaining_items_length=1800,
        )
        _bg, fg = format_overlay(status, default_config)
        assert "Sunday Service" in fg

    def test_position_tag_applied(self):
        status = LiveStatus(is_live=False, message="Test")

        for pos in ["top-left", "top-right", "bottom-left", "bottom-right"]:
            cfg = OverlayConfig(position=pos)
            bg, fg = format_overlay(status, cfg)
            # Both layers are full ASS scripts containing the \an tag
            assert "\\an" in fg
            assert "\\an" in bg

    def test_font_size_applied(self):
        status = LiveStatus(is_live=False, message="Test")
        cfg = OverlayConfig(font_size=100, font_size_title=50, font_size_info=30)
        _bg, fg = format_overlay(status, cfg)
        assert "\\fs50" in fg  # font_size_title for message

    def test_item_mode_shows_item_info(self):
        now = datetime.now(timezone.utc)
        cfg = OverlayConfig(timer_mode="item", show_description=True)
        status = LiveStatus(
            is_live=True,
            item_title="Sermon",
            item_description="Pastor John",
            item_end_time=now + timedelta(minutes=25),
        )
        _bg, fg = format_overlay(status, cfg)
        assert "Sermon" in fg
        assert "Pastor John" in fg

    def test_bg_layer_has_box_border(self, default_config):
        status = LiveStatus(is_live=False, message="Test")
        bg, _fg = format_overlay(status, default_config)
        # bg layer uses BorderStyle 3 (rectangle) with xbord/ybord
        # Style line has border_style as the 16th field (0-indexed)
        for line in bg.split("\n"):
            if line.startswith("Style:"):
                parts = line.split(",")
                assert parts[15] == "3"  # BorderStyle field
                break
        assert "\\xbord" in bg
        assert "\\ybord" in bg
        assert "\\1a&HFF" in bg  # text invisible

    def test_fg_layer_always_opaque(self, default_config):
        status = LiveStatus(is_live=False, message="Test")
        _bg, fg = format_overlay(status, default_config)
        # fg layer uses BorderStyle 1 (outline)
        for line in fg.split("\n"):
            if line.startswith("Style:"):
                parts = line.split(",")
                assert parts[15] == "1"  # BorderStyle field
                break
        # fg layer should NOT have any alpha tags
        assert "\\1a" not in fg
        assert "\\3a" not in fg

    def test_both_layers_are_full_ass_scripts(self, default_config):
        status = LiveStatus(is_live=False, message="Test")
        bg, fg = format_overlay(status, default_config)
        for layer in (bg, fg):
            assert "[Script Info]" in layer
            assert "PlayResX:" in layer
            assert "[V4+ Styles]" in layer
            assert "[Events]" in layer
            assert "Dialogue:" in layer

    def test_resolution_parameter(self):
        status = LiveStatus(is_live=False, message="Test")
        cfg = OverlayConfig()
        bg, fg = format_overlay(status, cfg, resolution=(3840, 2160))
        assert "PlayResX: 3840" in bg
        assert "PlayResY: 2160" in bg
        assert "PlayResX: 3840" in fg

    def test_margin_mapping_bottom_right(self):
        status = LiveStatus(is_live=False, message="Test")
        cfg = OverlayConfig(position="bottom-right")
        bg, _fg = format_overlay(status, cfg)
        # bottom-right: MarginR > 0, MarginL = 0 in style line
        # Parse the Style line
        for line in bg.split("\n"):
            if line.startswith("Style:"):
                parts = line.split(",")
                # MarginL is index 19, MarginR is index 20
                margin_l = int(parts[19])
                margin_r = int(parts[20])
                assert margin_l == 0
                assert margin_r > 0
                break

    def test_margin_mapping_bottom_left(self):
        status = LiveStatus(is_live=False, message="Test")
        cfg = OverlayConfig(position="bottom-left")
        bg, _fg = format_overlay(status, cfg)
        for line in bg.split("\n"):
            if line.startswith("Style:"):
                parts = line.split(",")
                margin_l = int(parts[19])
                margin_r = int(parts[20])
                assert margin_l > 0
                assert margin_r == 0
                break


class TestOverlayUpdater:
    """Test OverlayUpdater.run() loop behaviour."""

    @pytest.fixture
    def mock_mpv(self):
        mpv = MagicMock()
        mpv.set_overlay = AsyncMock()
        mpv.remove_overlay = AsyncMock()
        mpv.overlay_resolution = (1920, 1080)
        return mpv

    @pytest.fixture
    def mock_pco(self):
        pco = MagicMock()
        pco.credential_error = ""
        pco.get_live_status = AsyncMock(return_value=LiveStatus(
            is_live=False, message="Not live",
        ))
        return pco

    @pytest.fixture
    def config(self):
        cfg = Config()
        cfg.overlay.enabled = True
        cfg.pco.poll_interval = 5
        return cfg

    @pytest.mark.asyncio
    async def test_run_polls_pco_and_pushes_overlay(self, mock_mpv, mock_pco, config):
        updater = OverlayUpdater(mock_mpv, mock_pco, config)

        async def stop_after_tick():
            await asyncio.sleep(0.05)
            updater._running = False

        await asyncio.gather(updater.run(), stop_after_tick())

        mock_pco.get_live_status.assert_awaited()
        # Should push both bg + fg layers
        assert mock_mpv.set_overlay.await_count >= 2
        # Should remove both overlay layers on stop
        assert mock_mpv.remove_overlay.await_count == 2

    @pytest.mark.asyncio
    async def test_run_passes_ass_script_true(self, mock_mpv, mock_pco, config):
        updater = OverlayUpdater(mock_mpv, mock_pco, config)

        async def stop_after_tick():
            await asyncio.sleep(0.05)
            updater._running = False

        await asyncio.gather(updater.run(), stop_after_tick())

        # All set_overlay calls should use ass_script=True
        for call in mock_mpv.set_overlay.call_args_list:
            assert call.kwargs.get("ass_script") is True

    @pytest.mark.asyncio
    async def test_run_handles_pco_error_without_crashing(self, mock_mpv, mock_pco, config):
        mock_pco.get_live_status = AsyncMock(side_effect=Exception("API down"))
        updater = OverlayUpdater(mock_mpv, mock_pco, config)

        async def stop_after_tick():
            await asyncio.sleep(0.05)
            updater._running = False

        # Should not raise
        await asyncio.gather(updater.run(), stop_after_tick())
        # Overlay should still have been pushed (using cached status)
        mock_mpv.set_overlay.assert_awaited()

    @pytest.mark.asyncio
    async def test_stop_removes_overlay_and_exits(self, mock_mpv, mock_pco, config):
        updater = OverlayUpdater(mock_mpv, mock_pco, config)
        updater.start_task()
        await asyncio.sleep(0.05)

        assert updater.running is True
        await updater.stop()
        assert updater.running is False
        assert mock_mpv.remove_overlay.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_poll_on_credential_error(self, mock_mpv, mock_pco, config):
        mock_pco.credential_error = "Authentication failed (401)"
        updater = OverlayUpdater(mock_mpv, mock_pco, config)

        async def stop_after_tick():
            await asyncio.sleep(0.05)
            updater._running = False

        await asyncio.gather(updater.run(), stop_after_tick())
        # Should NOT have called get_live_status due to credential_error
        mock_pco.get_live_status.assert_not_awaited()


# ── OverlayUpdater Integration (shallow-mock) ────────────────────────────


def _attach_mock_writer(mgr: MpvManager) -> MagicMock:
    """Attach a mock writer to the manager and return it."""
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    mgr._writer = writer
    return writer


class TestOverlayUpdaterIntegration:
    """End-to-end: format_overlay() → set_overlay() → _send() → JSON bytes.

    Create a real MpvManager with a mock writer, call format_overlay() with
    realistic LiveStatus data, pass the result through set_overlay() → _send(),
    and verify the JSON payload contains the expected ASS content.
    """

    async def _resolve_after(self, mgr: MpvManager):
        await asyncio.sleep(0.01)
        rid = mgr._request_id
        fut = mgr._pending.get(rid)
        if fut and not fut.done():
            fut.set_result(None)

    async def test_format_overlay_to_ipc_payload(self):
        cfg = Config()
        cfg.overlay.enabled = True
        cfg.overlay.position = "bottom-right"
        mgr = MpvManager(cfg)
        writer = _attach_mock_writer(mgr)

        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            plan_title="Sunday Service",
            item_title="Worship",
            item_end_time=now + timedelta(minutes=10),
            remaining_items_length=1800,
        )
        _bg_ass, fg_ass = format_overlay(status, cfg.overlay, mgr.overlay_resolution)

        task = asyncio.create_task(self._resolve_after(mgr))
        await mgr.set_overlay(OVERLAY_FG_ID, fg_ass, ass_script=True)
        await task

        raw = writer.write.call_args[0][0]
        msg = json.loads(raw.decode().strip())
        cmd = msg["command"]
        assert isinstance(cmd, dict)
        assert cmd["name"] == "osd-overlay"
        assert cmd["format"] == "ass"
        assert "Sunday Service" in cmd["data"]
        assert r"\an3" in cmd["data"]  # bottom-right position tag
        assert "[Script Info]" in cmd["data"]

    async def test_not_live_overlay_to_ipc_payload(self):
        cfg = Config()
        cfg.overlay.enabled = True
        mgr = MpvManager(cfg)
        writer = _attach_mock_writer(mgr)

        status = LiveStatus(
            is_live=False,
            message="No live service",
            plan_title="Evening Prayer",
        )
        _bg_ass, fg_ass = format_overlay(status, cfg.overlay, mgr.overlay_resolution)

        task = asyncio.create_task(self._resolve_after(mgr))
        await mgr.set_overlay(OVERLAY_FG_ID, fg_ass, ass_script=True)
        await task

        raw = writer.write.call_args[0][0]
        msg = json.loads(raw.decode().strip())
        assert "No live service" in msg["command"]["data"]
        assert "Evening Prayer" in msg["command"]["data"]

    async def test_single_tick_poll_format_push(self):
        """One real tick of OverlayUpdater.run() with writer-mocked MpvManager.

        Verifies the PCO poll → format → IPC push chain end-to-end.
        """
        cfg = Config()
        cfg.overlay.enabled = True
        cfg.pco.poll_interval = 5
        mgr = MpvManager(cfg)
        writer = _attach_mock_writer(mgr)

        now = datetime.now(timezone.utc)
        live_status = LiveStatus(
            is_live=True,
            plan_title="Morning Worship",
            item_title="Opening Song",
            item_end_time=now + timedelta(minutes=5),
            remaining_items_length=600,
        )

        mock_pco = MagicMock()
        mock_pco.credential_error = ""
        mock_pco.get_live_status = AsyncMock(return_value=live_status)

        updater = OverlayUpdater(mgr, mock_pco, cfg)

        # Auto-resolve any pending futures so _send() doesn't block
        async def auto_resolve():
            while True:
                await asyncio.sleep(0.005)
                for rid, fut in list(mgr._pending.items()):
                    if not fut.done():
                        fut.set_result(None)

        async def stop_after_tick():
            await asyncio.sleep(0.05)
            updater._running = False

        resolver = asyncio.create_task(auto_resolve())
        await asyncio.gather(updater.run(), stop_after_tick())
        resolver.cancel()

        # PCO was polled
        mock_pco.get_live_status.assert_awaited()
        # Writer received IPC bytes — find the set_overlay call
        assert writer.write.called
        overlay_msgs = []
        for call in writer.write.call_args_list:
            raw = call[0][0]
            msg = json.loads(raw.decode().strip())
            cmd = msg.get("command", {})
            if isinstance(cmd, dict) and cmd.get("format") == "ass":
                overlay_msgs.append(msg)
        assert len(overlay_msgs) >= 2, "Expected bg + fg set_overlay IPC calls"
        # Both layers are full ASS scripts
        assert overlay_msgs[0]["command"]["name"] == "osd-overlay"
        assert "[Script Info]" in overlay_msgs[0]["command"]["data"]
        assert "Morning Worship" in overlay_msgs[1]["command"]["data"]
