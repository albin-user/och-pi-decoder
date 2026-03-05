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

OVERLAY_ID = 1       # background box layer
OVERLAY_FG_ID = 2    # foreground text layer

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


def _text_bord(fs: int) -> int:
    """Thin text outline — ~2% of font size, minimum 1."""
    return max(1, round(fs * 0.02))


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


BOX_MAX_CHARS = 25  # fixed box width in characters at font_size_title


def _truncate(text: str, max_chars: int) -> str:
    """Truncate *text* to *max_chars*, adding ellipsis if needed."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _overlay_layout(
    res_x: int, res_y: int,
    fs_title: int,
    num_lines: int,
    line_sizes: list[int],
) -> dict:
    """Calculate box size, edge gap, and text padding.

    Width is fixed at BOX_MAX_CHARS characters at *fs_title*.
    Height is dynamic based on the font sizes of lines actually present.

    Returns dict with: xbord, ybord, edge_x, edge_y, text_margin_x, text_margin_y
    """
    # Edge gap: 1.5% of screen
    edge_x = int(res_x * 0.015)
    edge_y = int(res_y * 0.015)

    # Inner padding: 0.8% of screen
    pad_x = int(res_x * 0.008)
    pad_y = int(res_y * 0.008)

    # Fixed box width based on title font size
    char_w = 0.55
    text_w = BOX_MAX_CHARS * fs_title * char_w
    xbord = int(text_w + pad_x * 2)

    # Height: sum of actual line heights + inner padding top & bottom
    line_h = 1.3
    text_h = sum(fs * line_h for fs in line_sizes)
    ybord = int(text_h + pad_y * 2)

    # Text margins: edge_gap + inner padding
    text_margin_x = edge_x + pad_x
    text_margin_y = edge_y + pad_y

    return {
        "xbord": xbord, "ybord": ybord,
        "edge_x": edge_x, "edge_y": edge_y,
        "text_margin_x": text_margin_x, "text_margin_y": text_margin_y,
    }


def _position_coords(
    position: str, layout: dict, res_x: int, res_y: int,
) -> tuple[int, int, int, int]:
    """Return (box_x, box_y, text_x, text_y)."""
    edge_x, edge_y = layout["edge_x"], layout["edge_y"]
    xbord, ybord = layout["xbord"], layout["ybord"]

    # Box top-left corner
    box_x = edge_x if "left" in position else res_x - edge_x - xbord
    box_y = edge_y if "top" in position else res_y - edge_y - ybord

    # Text anchor point (matches \an tag alignment)
    tmx, tmy = layout["text_margin_x"], layout["text_margin_y"]
    text_x = tmx if "left" in position else res_x - tmx
    text_y = tmy if "top" in position else res_y - tmy

    return box_x, box_y, text_x, text_y


def _build_bg_events(
    position: str,
    bg_alpha: str,
    layout: dict,
    res_x: int,
    res_y: int,
) -> str:
    """Build ass-events text for the background box layer using \\p1 drawing."""
    box_x, box_y, _, _ = _position_coords(position, layout, res_x, res_y)
    w, h = layout["xbord"], layout["ybord"]
    # Trim the box away from the text anchor corner so the background
    # is slightly smaller than the text area.
    trim_x = int(w * 0.10)
    trim_y = int(h * 0.20)
    if "right" in position:
        box_x += trim_x          # shrink from the left edge
    else:
        pass                      # left-anchored: keep left edge, shrink right (w shrinks)
    if "bottom" in position:
        box_y += trim_y           # shrink from the top edge
    else:
        pass                      # top-anchored: keep top edge, shrink bottom (h shrinks)
    w -= trim_x
    h -= trim_y
    return (
        f"{{\\an7\\pos({box_x},{box_y})\\p1"
        f"\\1c&H000000&\\1a{bg_alpha}"
        f"\\bord0\\shad0}}"
        f"m 0 0 l {w} 0 {w} {h} 0 {h}"
    )


def _build_fg_events(
    fg_event_text: str,
    position: str,
    layout: dict,
    res_x: int,
    res_y: int,
) -> str:
    """Build ass-events text for the foreground text layer with explicit \\pos."""
    _, _, text_x, text_y = _position_coords(position, layout, res_x, res_y)
    # Insert \pos after opening { before the existing override tags
    return "{" + f"\\pos({text_x},{text_y})" + fg_event_text[1:]


def format_overlay(
    status: LiveStatus,
    cfg: OverlayConfig,
    resolution: tuple[int, int] = (1920, 1080),
) -> tuple[str, str]:
    """Build (background_events, foreground_events) ass-events strings.

    Background layer: \\p1 drawing of a semi-transparent filled rectangle.
    Foreground layer: positioned text with thin outline.
    """
    pos_tag = _POSITION_MAP.get(cfg.position, r"\an3")
    fs = cfg.font_size
    bg_alpha = _ass_alpha(cfg.transparency)
    res_x, res_y = resolution

    now = datetime.now(timezone.utc)

    fs_title = cfg.font_size_title
    fs_info = cfg.font_size_info

    # ── Build foreground event text (same logic as before, without bg) ──

    # max displayable chars at each font size, scaled relative to title
    max_title = BOX_MAX_CHARS
    max_info = int(BOX_MAX_CHARS * fs_title / fs_info) if fs_info else max_title

    if not status.is_live:
        msg = _truncate(status.message or "Waiting...", max_title)
        fg_parts = [
            f"{{{pos_tag}\\fs{fs_title}"
            f"\\1c&HFFFFFF&\\3c&H000000&"
            f"\\bord{_text_bord(fs_title)}\\shad0}}{msg}"
        ]
        line_sizes = [fs_title]
        if status.plan_title:
            pt = _truncate(status.plan_title, max_info)
            fg_parts.append(
                f"\\N{{\\fs{fs_info}\\bord{_text_bord(fs_info)}}}{pt}"
            )
            line_sizes.append(fs_info)
        layout = _overlay_layout(res_x, res_y, fs_title, len(line_sizes), line_sizes)

        bg = _build_bg_events(cfg.position, bg_alpha, layout, res_x, res_y)
        fg = _build_fg_events("".join(fg_parts), cfg.position, layout, res_x, res_y)
        return (bg, fg)

    if status.finished:
        overtime_text = "FINISHED"
        service_end = status.service_end_time
        if service_end:
            delta = (now - service_end).total_seconds()
            if delta > 0:
                overtime_text = f"-{format_countdown(delta)}"

        plan_label = _truncate(status.plan_title or "Service", max_title)
        fg_parts = [
            f"{{{pos_tag}\\fs{fs}\\b1"
            f"\\1c&H0000FF&\\3c&H000000&"
            f"\\bord{_text_bord(fs)}\\shad0}}{overtime_text}"
            f"\\N{{\\fs{fs_title}\\b0\\bord{_text_bord(fs_title)}}}{plan_label}"
            f"\\N{{\\fs{fs_info}\\bord{_text_bord(fs_info)}}}OVERTIME"
        ]
        layout = _overlay_layout(res_x, res_y, fs_title, 3, [fs, fs_title, fs_info])

        bg = _build_bg_events(cfg.position, bg_alpha, layout, res_x, res_y)
        fg = _build_fg_events("".join(fg_parts), cfg.position, layout, res_x, res_y)
        return (bg, fg)

    # ── Live, pre/post service item ─────────────────────────────────

    if status.service_position in ("pre", "post"):
        pos_label = "Pre-service" if status.service_position == "pre" else "Post-service"
        fs_prepost = int(fs * 0.8)
        plan_label = _truncate(status.plan_title or "Service", max_title)
        item_label = _truncate(status.item_title or pos_label, max_info)

        fg_parts = [
            f"{{{pos_tag}\\fs{fs_prepost}\\b1"
            f"\\1c&HFFFFFF&\\3c&H000000&"
            f"\\bord{_text_bord(fs_prepost)}\\shad0}}{pos_label}",
        ]
        line_sizes = [fs_prepost, fs_title, fs_info]

        fg_parts.append(
            f"\\N{{\\fs{fs_title}\\b0\\1c&HFFFFFF&\\bord{_text_bord(fs_title)}}}{plan_label}"
        )
        fg_parts.append(
            f"\\N{{\\fs{fs_info}\\1c&HFFFFFF&\\bord{_text_bord(fs_info)}}}{item_label}"
        )

        layout = _overlay_layout(res_x, res_y, fs_title, len(line_sizes), line_sizes)
        bg = _build_bg_events(cfg.position, bg_alpha, layout, res_x, res_y)
        fg = _build_fg_events("".join(fg_parts), cfg.position, layout, res_x, res_y)
        return (bg, fg)

    # ── Live, not finished ───────────────────────────────────────────

    if cfg.timer_mode == "item" and status.item_end_time:
        remaining = (status.item_end_time - now).total_seconds()
        countdown = format_countdown(remaining)
        label = _truncate(status.item_title or "Current Item", max_title)
    elif status.item_end_time is not None:
        item_remaining = (status.item_end_time - now).total_seconds()
        if item_remaining >= 0:
            remaining = item_remaining + status.remaining_items_length
        elif status.remaining_items_length > 0:
            remaining = status.remaining_items_length
        else:
            remaining = item_remaining
        countdown = format_countdown(remaining)
        label = _truncate(status.plan_title or "Service", max_title)
    else:
        remaining = 0
        countdown = "--:--"
        label = _truncate(status.plan_title or "Service", max_title)

    # color: white normally, red if overtime
    if remaining < 0:
        color = "\\1c&H0000FF&"  # red in BGR
    else:
        color = "\\1c&HFFFFFF&"

    fg_parts = [
        f"{{{pos_tag}\\fs{fs}\\b1"
        f"{color}\\3c&H000000&"
        f"\\bord{_text_bord(fs)}\\shad0}}{countdown}",
    ]
    line_sizes = [fs, fs_title]

    # title line (reset to white in case countdown was red)
    fg_parts.append(f"\\N{{\\fs{fs_title}\\b0\\1c&HFFFFFF&\\bord{_text_bord(fs_title)}}}{label}")

    # description (item mode only)
    if cfg.show_description and cfg.timer_mode == "item" and status.item_description:
        desc = _truncate(status.item_description, max_info)
        fg_parts.append(f"\\N{{\\fs{fs_info}\\1c&HFFFFFF&\\bord{_text_bord(fs_info)}}}{desc}")
        line_sizes.append(fs_info)

    # schedule status
    if cfg.show_service_end and status.item_end_time is not None:
        try:
            local_tz = ZoneInfo(cfg.timezone)
            if cfg.timer_mode == "item":
                svc_item_remaining = max(0.0, (status.item_end_time - now).total_seconds())
                svc_remaining = svc_item_remaining + status.remaining_items_length
            else:
                svc_remaining = remaining
            end_label = _format_schedule_status(
                svc_remaining, status.planned_service_end, now, local_tz
            )
            fg_parts.append(f"\\N{{\\fs{fs_info}\\1c&HFFFFFF&\\bord{_text_bord(fs_info)}}}{end_label}")
            line_sizes.append(fs_info)
        except Exception:
            pass

    layout = _overlay_layout(res_x, res_y, fs_title, len(line_sizes), line_sizes)

    bg = _build_bg_events(cfg.position, bg_alpha, layout, res_x, res_y)
    fg = _build_fg_events("".join(fg_parts), cfg.position, layout, res_x, res_y)
    return (bg, fg)


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
        self._poll_task: asyncio.Task | None = None
        self._last_status: LiveStatus = LiveStatus(message="Initializing...")
        self._last_overlay_warn: float = 0.0

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
        import time as _time
        # Align first tick to the next wall-clock second boundary so the
        # displayed countdown flips in sync with real seconds.
        frac = _time.time() % 1.0
        align_delay = (1.0 - frac) % 1.0
        next_tick = _time.monotonic() + align_delay
        last_poll = _time.monotonic() - poll_interval  # trigger immediate first poll

        try:
            while self._running:
                now_mono = _time.monotonic()

                # Fire PCO poll as background task so it doesn't block ticks
                if now_mono - last_poll >= poll_interval:
                    last_poll = now_mono
                    if not self._pco.credential_error:
                        if self._poll_task is None or self._poll_task.done():
                            self._poll_task = asyncio.create_task(self._do_poll())

                # Format and push overlay every second
                if self._config.overlay.enabled:
                    try:
                        res = self._mpv.overlay_resolution
                        bg_ass, fg_ass = format_overlay(
                            self._last_status, self._config.overlay, res,
                        )
                        await asyncio.gather(
                            self._mpv.set_overlay(OVERLAY_ID, bg_ass),
                            self._mpv.set_overlay(OVERLAY_FG_ID, fg_ass),
                        )
                    except Exception:
                        now_t = _time.monotonic()
                        if now_t - self._last_overlay_warn > 30.0:
                            self._last_overlay_warn = now_t
                            log.warning("Overlay push failed", exc_info=True)
                        else:
                            log.debug("Overlay push failed", exc_info=True)

                # Schedule next tick at a fixed 1 s cadence
                next_tick += 1.0
                delay = max(0, next_tick - _time.monotonic())
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            # remove both overlay layers on stop
            for oid in (OVERLAY_ID, OVERLAY_FG_ID):
                try:
                    await self._mpv.remove_overlay(oid)
                except Exception:
                    pass

    async def _do_poll(self) -> None:
        """Run a single PCO poll in the background."""
        try:
            self._last_status = await self._pco.get_live_status()
        except Exception:
            log.exception("PCO poll failed")

    async def stop(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
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
