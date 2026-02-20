"""Entry point — start all components."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys

import uvicorn

from pi_decoder.config import Config, load_config, save_config
from pi_decoder.mpv_manager import MpvManager
from pi_decoder.overlay import OverlayUpdater
from pi_decoder.pco_client import PCOClient
from pi_decoder.web.app import create_app

log = logging.getLogger("pi_decoder")

CONFIG_PATH = os.environ.get("PI_DECODER_CONFIG", "/etc/pi-decoder/config.toml")


async def async_main() -> None:
    config = load_config(CONFIG_PATH)
    log.info("Config loaded from %s", CONFIG_PATH)
    log.info("Stream URL: %s", config.stream.url)
    log.info("Overlay enabled: %s", config.overlay.enabled)

    # Sync system hostname if it doesn't match configured name
    from pi_decoder.hostname import sanitize_hostname, set_hostname
    expected = sanitize_hostname(config.general.name)
    if socket.gethostname() != expected:
        await set_hostname(config.general.name)
        log.info("Hostname synced to '%s'", expected)

    mpv = MpvManager(config)
    pco: PCOClient | None = None
    overlay: OverlayUpdater | None = None

    # PCO + overlay (only if overlay enabled and credentials present)
    if config.overlay.enabled and config.pco.app_id:
        pco = PCOClient(config)
        overlay = OverlayUpdater(mpv, pco, config)
    elif config.overlay.enabled:
        log.warning("Overlay enabled but PCO credentials missing — overlay will not start")
        # still create PCO client without credentials so web UI can test
        pco = PCOClient(config)

    # Turn on TV and switch to Pi's HDMI input (background, don't block startup)
    async def _cec_startup():
        try:
            from pi_decoder import cec
            await cec.power_on()
            await asyncio.sleep(10)
            await cec.active_source()
            log.info("CEC: TV powered on and input switched to Pi")
        except Exception as e:
            log.warning("CEC startup failed (TV may not support CEC): %s", e)

    asyncio.create_task(_cec_startup())

    # Start mpv
    await mpv.start()

    # Start overlay loop
    if overlay:
        overlay.start_task()
        log.info("Overlay updater started")

    # Build FastAPI app
    web_app = create_app(mpv, pco, overlay, config, config_path=CONFIG_PATH)

    # Run uvicorn
    uv_config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=config.web.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    async def wait_for_shutdown():
        await shutdown_event.wait()
        server.should_exit = True

    shutdown_task = asyncio.create_task(wait_for_shutdown())

    try:
        await server.serve()
    finally:
        shutdown_task.cancel()
        log.info("Shutting down...")
        # Use app accessors to pick up lazily-created objects
        _overlay = web_app._get_overlay()  # type: ignore[attr-defined]
        _pco = web_app._get_pco()  # type: ignore[attr-defined]
        if _overlay:
            await _overlay.stop()
        if _pco:
            await _pco.close()
        await mpv.stop()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
