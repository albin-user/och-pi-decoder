"""ASS overlay formatter and push loop for mpv."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from pi_decoder.config import Config, OverlayConfig
from pi_decoder.mpv_manager import MpvManager
from pi_decoder.pco_client import PCOClient, LiveStatus

log = logging.getLogger(__name__)

OVERLAY_ID = 1

# ASS \an tag mapping
_POSITION_MAP = {
    "bottom-left": r"\an1",
    "bottom-right": r"\an3",
    "top-left": r"\an7",
    "top-right": r"\an9",
}


def format_countdown(total_seconds: float) -> str:
    """Format seconds to HH:MM:SS or MM:SS, with sign for negative."""
    negative = total_seconds < 0
    s = int(abs(total_seconds))
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    sign = "-" if negative else ""
    if h > 0:
        return f"{sign}{h}:{m:02d}:{sec:02d}"
    return f"{sign}{m:02d}:{sec:02d}"


def _ass_alpha(transparency: float) -> str:
    """Convert 0.0–1.0 transparency to ASS alpha hex (00=opaque, FF=invisible)."""
    # transparency 0.0 → invisible → alpha FF
    # transparency 1.0 → fully opaque → alpha 00
    # So we invert: higher transparency value = more visible
    alpha_byte = int((1.0 - transparency) * 255)
    alpha_byte = max(0, min(255, alpha_byte))
    return f"&H{alpha_byte:02X}"


def _format_schedule_status(
    remaining: float,
    planned_service_end: datetime | None,
    now: datetime,
    local_tz: ZoneInfo,
) -> str:
    """Format 'Ends Xm ahead/behind at HH:MM'."""
    if remaining <= 0:
        return "OVERTIME"

    projected_end = now + timedelta(seconds=remaining)
    local_end = projected_end.astimezone(local_tz)
    end_time_str = local_end.strftime('%H:%M')

    if planned_service_end is None:
        return f"Ends at {end_time_str}"

    slip_seconds = (projected_end - planned_service_end).total_seconds()
    slip_minutes = int(abs(slip_seconds) / 60)

    if abs(slip_seconds) < 60:  # less than 1 minute difference
        return f"Ends on time at {end_time_str}"
    elif slip_seconds > 0:
        return f"Ends {slip_minutes}m behind at {end_time_str}"
    else:
        return f"Ends {slip_minutes}m ahead at {end_time_str}"


def format_overlay(status: LiveStatus, cfg: OverlayConfig) -> str:
    """Build an ASS events string from LiveStatus + overlay config."""
    pos_tag = _POSITION_MAP.get(cfg.position, r"\an3")
    fs = cfg.font_size
    bg_alpha = _ass_alpha(cfg.transparency)

    now = datetime.now(timezone.utc)

    fs_title = cfg.font_size_title
    fs_info = cfg.font_size_info

    if not status.is_live:
        # Not live — show message
        msg = status.message or "Waiting..."
        parts = [
            f"{{{pos_tag}\\fs{fs_title}\\1c&HFFFFFF&\\3c&H000000&\\bord2}}{msg}"
        ]
        if status.plan_title:
            parts.append(
                f"\\N{{\\fs{fs_info}}}{status.plan_title}"
            )
        return "".join(parts)

    if status.finished:
        # Service is over
        overtime_text = "FINISHED"
        service_end = status.service_end_time
        if service_end:
            delta = (now - service_end).total_seconds()
            if delta > 0:
                overtime_text = f"-{format_countdown(delta)}"

        return (
            f"{{{pos_tag}\\fs{fs}\\b1"
            f"\\1c&H0000FF&\\3c&H000000&\\4c&H000000&\\4a{bg_alpha}"
            f"\\bord3\\shad0}}{overtime_text}"
            f"\\N{{\\fs{fs_title}\\b0}}{status.plan_title}"
            f"\\N{{\\fs{fs_info}}}OVERTIME"
        )

    # ── Live, not finished ───────────────────────────────────────────

    if cfg.timer_mode == "item" and status.item_end_time:
        remaining = (status.item_end_time - now).total_seconds()
        countdown = format_countdown(remaining)
        label = status.item_title or "Current Item"
    elif status.item_end_time is not None:
        # Dynamic service remaining, computed every second between PCO polls.
        item_remaining = (status.item_end_time - now).total_seconds()
        if item_remaining >= 0:
            # Item on time: countdown = item remaining + future items
            remaining = item_remaining + status.remaining_items_length
        elif status.remaining_items_length > 0:
            # Item overtime, more items after: freeze countdown at future
            # items' total length.  "Ends HH:MM" extends because remaining
            # stays constant while `now` advances each second.
            remaining = status.remaining_items_length
        else:
            # Last item overtime: show negative countdown
            remaining = item_remaining
        countdown = format_countdown(remaining)
        label = status.plan_title or "Service"
    else:
        remaining = 0
        countdown = "--:--"
        label = status.plan_title or "Service"

    # color: white normally, red if overtime
    if remaining < 0:
        color = "\\1c&H0000FF&"  # red in BGR
    else:
        color = "\\1c&HFFFFFF&"

    parts = [
        f"{{{pos_tag}\\fs{fs}\\b1"
        f"{color}\\3c&H000000&\\4c&H000000&\\4a{bg_alpha}"
        f"\\bord3\\shad0}}{countdown}",
    ]

    # title line
    parts.append(f"\\N{{\\fs{fs_title}\\b0}}{label}")

    # description (item mode only)
    if cfg.show_description and cfg.timer_mode == "item" and status.item_description:
        desc = status.item_description[:50]
        parts.append(f"\\N{{\\fs{fs_info}}}{desc}")

    # schedule status — shows "Ends Xm ahead/behind at HH:MM"
    if cfg.show_service_end and status.item_end_time is not None:
        try:
            local_tz = ZoneInfo(cfg.timezone)
            # For item mode, compute service remaining separately
            if cfg.timer_mode == "item":
                svc_item_remaining = max(0.0, (status.item_end_time - now).total_seconds())
                svc_remaining = svc_item_remaining + status.remaining_items_length
            else:
                svc_remaining = remaining
            end_label = _format_schedule_status(
                svc_remaining, status.planned_service_end, now, local_tz
            )
            parts.append(f"\\N{{\\fs{fs_info}}}{end_label}")
        except Exception:
            pass

    return "".join(parts)


class OverlayUpdater:
    """Periodically poll PCO and push ASS overlay to mpv."""

    def __init__(
        self,
        mpv: MpvManager,
        pco: PCOClient,
        config: Config,
    ) -> None:
        self._mpv = mpv
        self._pco = pco
        self._config = config
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_status: LiveStatus = LiveStatus(message="Initializing...")

    @property
    def last_status(self) -> LiveStatus:
        return self._last_status

    @property
    def running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """Main loop: poll PCO every N seconds, push overlay every 1 s."""
        self._running = True
        poll_interval = self._config.pco.poll_interval
        seconds_since_poll = poll_interval  # trigger immediate first poll

        try:
            while self._running:
                # Poll PCO API periodically
                if seconds_since_poll >= poll_interval:
                    if self._pco.credential_error:
                        # Don't hammer API with bad credentials
                        seconds_since_poll = 0
                    else:
                        try:
                            self._last_status = await self._pco.get_live_status()
                        except Exception:
                            log.exception("PCO poll failed")
                        seconds_since_poll = 0

                # Format and push overlay every second
                if self._config.overlay.enabled:
                    try:
                        ass = format_overlay(self._last_status, self._config.overlay)
                        await self._mpv.set_overlay(OVERLAY_ID, ass)
                    except Exception:
                        log.debug("Overlay push failed", exc_info=True)

                await asyncio.sleep(1)
                seconds_since_poll += 1
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            # remove overlay on stop
            try:
                await self._mpv.remove_overlay(OVERLAY_ID)
            except Exception:
                pass

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def start_task(self) -> asyncio.Task:
        """Start the overlay loop as a background task and return it."""
        self._task = asyncio.create_task(self.run())
        return self._task
