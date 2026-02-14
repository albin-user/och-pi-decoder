"""Tests for main entry point."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_decoder.config import Config


class TestRun:
    """Test the run() entry point."""

    @patch("pi_decoder.main.asyncio.run")
    def test_run_calls_asyncio_run(self, mock_arun):
        from pi_decoder.main import run
        run()
        mock_arun.assert_called_once()

    @patch("pi_decoder.main.asyncio.run", side_effect=KeyboardInterrupt)
    def test_run_handles_keyboard_interrupt(self, mock_arun):
        from pi_decoder.main import run
        # Should not raise
        run()


class TestAsyncMain:
    """Test async_main startup sequence with mocked components."""

    @pytest.fixture
    def mock_deps(self, tmp_path):
        """Patch all heavy dependencies for async_main."""
        config_path = str(tmp_path / "config.toml")
        cfg = Config()
        cfg.general.name = "test-decoder"
        cfg.stream.url = "rtmp://test/live"
        cfg.overlay.enabled = False

        patches = {
            "load_config": patch("pi_decoder.main.load_config", return_value=cfg),
            "mpv_cls": patch("pi_decoder.main.MpvManager"),
            "pco_cls": patch("pi_decoder.main.PCOClient"),
            "overlay_cls": patch("pi_decoder.main.OverlayUpdater"),
            "create_app": patch("pi_decoder.main.create_app"),
            "uvicorn_config": patch("pi_decoder.main.uvicorn.Config"),
            "uvicorn_server": patch("pi_decoder.main.uvicorn.Server"),
            "set_hostname": patch("pi_decoder.hostname.set_hostname", new_callable=AsyncMock, return_value="test-decoder"),
            "sanitize": patch("pi_decoder.hostname.sanitize_hostname", return_value="test-decoder"),
            "config_path": patch("pi_decoder.main.CONFIG_PATH", config_path),
            "gethostname": patch("pi_decoder.main.socket.gethostname", return_value="old-name"),
            "cec_power_on": patch("pi_decoder.cec.power_on", new_callable=AsyncMock),
            "cec_active_source": patch("pi_decoder.cec.active_source", new_callable=AsyncMock),
        }
        mocks = {}
        for name, p in patches.items():
            mocks[name] = p.start()

        # Configure MpvManager mock
        mock_mpv = MagicMock()
        mock_mpv.start = AsyncMock()
        mock_mpv.stop = AsyncMock()
        mocks["mpv_cls"].return_value = mock_mpv
        mocks["mock_mpv"] = mock_mpv

        # Configure PCOClient mock
        mock_pco = MagicMock()
        mock_pco.close = AsyncMock()
        mocks["pco_cls"].return_value = mock_pco

        # Configure OverlayUpdater mock
        mock_overlay = MagicMock()
        mock_overlay.stop = AsyncMock()
        mock_overlay.start_task = MagicMock()
        mocks["overlay_cls"].return_value = mock_overlay

        # Configure uvicorn server mock to exit immediately
        mock_server = MagicMock()
        mock_server.serve = AsyncMock()
        mock_server.should_exit = False
        mocks["uvicorn_server"].return_value = mock_server
        mocks["mock_server"] = mock_server

        mocks["config"] = cfg

        yield mocks

        for p in patches.values():
            p.stop()

    @pytest.mark.asyncio
    async def test_startup_loads_config(self, mock_deps):
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["load_config"].assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_starts_mpv(self, mock_deps):
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["mock_mpv"].start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_stops_mpv_on_shutdown(self, mock_deps):
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["mock_mpv"].stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_creates_app(self, mock_deps):
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["create_app"].assert_called_once()

    @pytest.mark.asyncio
    async def test_overlay_not_created_when_disabled(self, mock_deps):
        mock_deps["config"].overlay.enabled = False
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["overlay_cls"].assert_not_called()

    @pytest.mark.asyncio
    async def test_overlay_created_when_enabled_with_credentials(self, mock_deps):
        mock_deps["config"].overlay.enabled = True
        mock_deps["config"].pco.app_id = "test_app"
        mock_deps["config"].pco.secret = "test_secret"
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["overlay_cls"].assert_called_once()

    @pytest.mark.asyncio
    async def test_pco_created_without_overlay_when_no_credentials(self, mock_deps):
        mock_deps["config"].overlay.enabled = True
        mock_deps["config"].pco.app_id = ""
        from pi_decoder.main import async_main
        await async_main()
        # PCO client created for web UI testing, but overlay not created
        mock_deps["pco_cls"].assert_called_once()
        mock_deps["overlay_cls"].assert_not_called()

    @pytest.mark.asyncio
    async def test_hostname_sync_when_mismatch(self, mock_deps):
        # gethostname returns "old-name" by default, sanitize returns "test-decoder"
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["set_hostname"].assert_awaited()

    @pytest.mark.asyncio
    async def test_hostname_no_sync_when_matches(self, mock_deps):
        mock_deps["gethostname"].return_value = "test-decoder"
        from pi_decoder.main import async_main
        await async_main()
        mock_deps["set_hostname"].assert_not_awaited()
