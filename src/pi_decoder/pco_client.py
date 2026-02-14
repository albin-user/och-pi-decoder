"""Async Planning Center Online Services API client.

Uses the PCO Live controller API to track the actual service state in
real time, rather than estimating position from planned item lengths.

Plan discovery strategy (unified Track-One state machine):
  1. If we have a locked plan (active Live session found previously),
     poll its Live endpoint directly — one API call.
  2. Otherwise, discover plans across all service types in the folder:
     a. Fetch future plans (per_page=3) for each service type.
     b. Poll /live for all candidates, pick the plan with the most
        recent live_start_at (handles abandoned sessions correctly).
     c. Lock onto the winner.
  3. Once locked, stay locked until the Live session ends.
  4. Every 60s during TRACKING, do a full scan to catch abandoned-session
     takeover (operator left plan A live, started plan B).
  5. Uses search_mode config to determine folder vs service_type lookup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import httpx
import dateutil.parser

from pi_decoder.config import Config

log = logging.getLogger(__name__)

BASE_URL = "https://api.planningcenteronline.com/services/v2"

# Only check past plans within this window (seconds).  Prevents checking
# a plan from months ago when there are no recent services.
_PAST_PLAN_MAX_AGE = 43200  # 12 hours

# How often to do a full scan while tracking (seconds)
_TRACKING_FULL_SCAN_INTERVAL = 60


@dataclass
class LiveStatus:
    is_live: bool = False
    finished: bool = False
    plan_title: str = ""
    item_title: str = ""
    item_description: str = ""
    item_end_time: datetime | None = None
    service_end_time: datetime | None = None
    remaining_items_length: float = 0.0  # seconds of planned time for items after current
    message: str = ""
    # Extra fields from Live API (available for future overlay use)
    next_item_title: str = ""
    plan_index: int = 0
    plan_length: int = 0
    planned_service_end: datetime | None = None  # original planned end (PlanTime starts_at + during items)


class PCOClient:
    """Async client for Planning Center Services Live API."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._auth = (config.pco.app_id, config.pco.secret)
        self._folder_id = config.pco.folder_id
        self._service_type_id = config.pco.service_type_id
        self._search_mode = config.pco.search_mode
        self._cached_status: LiveStatus = LiveStatus(message="Initializing...")
        self._client: httpx.AsyncClient | None = None
        # Plan locking — avoids re-discovering mid-service
        self._locked_plan_id: str | None = None
        self._locked_st_id: str | None = None  # service_type_id of locked plan
        self._locked_service_start: datetime | None = None
        self._locked_planned_end: datetime | None = None
        self._locked_live_start_at: datetime | None = None
        self._seen_active_item: bool = False
        self._last_full_scan: float = 0
        self._credential_error: str = ""  # non-empty = stop polling
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=self._auth,
                timeout=10.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def update_credentials(
        self, app_id: str, secret: str, service_type_id: str,
        *, folder_id: str = "", search_mode: str = "",
    ) -> None:
        """Update credentials (used when config is changed via web UI)."""
        self._auth = (app_id, secret)
        self._service_type_id = service_type_id
        if folder_id is not None:
            self._folder_id = folder_id
        if search_mode:
            self._search_mode = search_mode
        self._locked_plan_id = None
        self._locked_st_id = None
        self._locked_service_start = None
        self._locked_planned_end = None
        self._locked_live_start_at = None
        self._seen_active_item = False
        self._credential_error = ""
        # force new client on next request; schedule close of old one
        if self._client and not self._client.is_closed:
            old = self._client
            self._client = None
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass  # no event loop — GC will clean up

    # ── public API ───────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        """Test credentials by fetching service types."""
        client = await self._get_client()
        try:
            resp = await client.get(f"{BASE_URL}/service_types")
            if resp.status_code == 401:
                return {"success": False, "error": "Authentication failed (401). Check App ID and Secret."}
            resp.raise_for_status()
            data = resp.json()
            service_types = []
            for st in data.get("data", []):
                service_types.append({
                    "id": st["id"],
                    "name": st["attributes"].get("name", "Unknown"),
                    "frequency": st["attributes"].get("frequency", "Unknown"),
                })
            return {"success": True, "service_types": service_types}
        except httpx.TimeoutException:
            return {"success": False, "error": "Connection timeout."}
        except httpx.ConnectError:
            return {"success": False, "error": "Connection error. Check internet."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_service_types(self) -> list[dict]:
        """Return list of service types."""
        result = await self.test_connection()
        return result.get("service_types", [])

    async def get_live_status(self) -> LiveStatus:
        """Get current service status via the PCO Live controller API.

        When a Live session is active, this returns real-time data about
        which item is currently live, when it started, and accurate
        countdown timers.  When no Live session is active, it falls back
        to showing the next upcoming plan with a "starts in" message.
        """
        if self._search_mode == "folder" and not self._folder_id:
            status = LiveStatus(message="No folder ID configured")
            self._cached_status = status
            return status
        if self._search_mode == "service_type" and not self._service_type_id:
            status = LiveStatus(message="No service type configured")
            self._cached_status = status
            return status

        try:
            status = await self._fetch_live_status()
            self._cached_status = status
            self._credential_error = ""  # clear on success
            return status
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                self._credential_error = f"Authentication failed ({e.response.status_code})"
                log.warning("PCO credential error: %s", self._credential_error)
                status = LiveStatus(message=self._credential_error)
                self._cached_status = status
                return status
            log.warning("PCO API error (%s): %s", e.response.status_code, e)
            return self._cached_status
        except httpx.TimeoutException as e:
            log.warning("PCO API timeout: %s", e)
            return self._cached_status
        except (KeyError, ValueError, TypeError) as e:
            log.warning("PCO response parse error: %s", e)
            return self._cached_status
        except Exception:
            log.exception("PCO unexpected error — returning cached status")
            return self._cached_status

    @property
    def credential_error(self) -> str:
        return self._credential_error

    @property
    def cached_status(self) -> LiveStatus:
        return self._cached_status

    # ── service type discovery ────────────────────────────────────────────

    async def _get_service_type_ids(self) -> list[str]:
        """Get service type IDs based on search_mode config."""
        if self._search_mode == "folder" and self._folder_id:
            client = await self._get_client()
            try:
                resp = await client.get(f"{BASE_URL}/folders/{self._folder_id}/service_types")
                resp.raise_for_status()
                data = resp.json()
                return [st["id"] for st in data.get("data", [])]
            except Exception:
                log.warning("Failed to fetch folder service types", exc_info=True)
                return []
        elif self._service_type_id:
            return [self._service_type_id]
        return []

    # ── plan discovery ───────────────────────────────────────────────────

    async def _fetch_live_status(self) -> LiveStatus:
        """Main dispatch: use locked plan or discover one."""
        async with self._lock:
            return await self._fetch_live_status_locked()

    async def _fetch_live_status_locked(self) -> LiveStatus:
        """Inner implementation, called under self._lock."""
        # Fast path: we already know which plan is live
        if self._locked_plan_id and self._locked_st_id:
            status = await self._poll_live(self._locked_plan_id, self._locked_st_id)
            if status.is_live or status.finished:
                # Periodic full scan to catch abandoned-session takeover
                import time
                now = time.time()
                if now - self._last_full_scan >= _TRACKING_FULL_SCAN_INTERVAL:
                    self._last_full_scan = now
                    better = await self._find_best_live_plan()
                    if (better and better[0] != self._locked_plan_id
                            and better[2] and self._locked_live_start_at
                            and better[2] > self._locked_live_start_at):
                        # Switch to the newer plan
                        plan_id, st_id, live_start_at, service_start, planned_end = better
                        log.info("Switching from plan %s to newer plan %s", self._locked_plan_id, plan_id)
                        self._locked_plan_id = plan_id
                        self._locked_st_id = st_id
                        self._locked_live_start_at = live_start_at
                        self._locked_service_start = service_start
                        self._locked_planned_end = planned_end
                        self._seen_active_item = False
                        status = await self._poll_live(plan_id, st_id)
                return status
            # Live session ended — unlock and re-discover
            log.info("Live session ended for plan %s — unlocking", self._locked_plan_id)
            self._locked_plan_id = None
            self._locked_st_id = None
            self._locked_service_start = None
            self._locked_planned_end = None
            self._locked_live_start_at = None
            self._seen_active_item = False

        return await self._discover_and_poll()

    async def _find_best_live_plan(self) -> tuple | None:
        """Scan all service types for the plan with the most recent live_start_at.

        Returns (plan_id, st_id, live_start_at, service_start, planned_end) or None.
        """
        client = await self._get_client()
        st_ids = await self._get_service_type_ids()

        best = None
        best_live_start = datetime.min.replace(tzinfo=timezone.utc)

        for stid in st_ids:
            try:
                resp = await client.get(
                    f"{BASE_URL}/service_types/{stid}/plans",
                    params={
                        "filter": "future",
                        "order": "sort_date",
                        "per_page": "3",
                        "include": "plan_times",
                    },
                )
                if resp.status_code in (401, 403):
                    self._credential_error = f"Authentication failed ({resp.status_code})"
                    log.warning("PCO credential error in plan scan: %s", self._credential_error)
                    return None
                if resp.status_code != 200:
                    continue
                data = resp.json()
                plans = data.get("data", [])
                plan_times_list = [
                    o for o in data.get("included", [])
                    if o.get("type") == "PlanTime"
                ]

                for plan in plans:
                    plan_id = plan["id"]
                    # Extract service times for this plan
                    plan_pt_ids = [
                        ref["id"] for ref in
                        plan.get("relationships", {}).get("plan_times", {}).get("data", [])
                    ]
                    plan_pts = [pt for pt in plan_times_list if pt["id"] in plan_pt_ids]
                    service_start, planned_end = self._extract_service_times(plan_pts)

                    # Poll /live for this plan
                    try:
                        live_resp = await client.get(
                            f"{BASE_URL}/service_types/{stid}/plans/{plan_id}/live",
                            params={"include": "items,current_item_time"},
                        )
                        if live_resp.status_code not in (200,):
                            continue
                        live_result = live_resp.json()
                        live_data = live_result.get("data", {})
                        rels = live_data.get("relationships", {})
                        cit_ref = rels.get("current_item_time", {}).get("data")
                        if not cit_ref:
                            continue

                        included = live_result.get("included", [])
                        cit_obj = next(
                            (o for o in included if o.get("type") == "ItemTime" and o.get("id") == cit_ref.get("id")),
                            None,
                        )
                        if not cit_obj:
                            continue

                        live_start_str = cit_obj.get("attributes", {}).get("live_start_at")
                        if not live_start_str:
                            continue

                        live_start_at = dateutil.parser.isoparse(live_start_str)
                        if live_start_at > best_live_start:
                            best_live_start = live_start_at
                            best = (plan_id, stid, live_start_at, service_start, planned_end)
                    except Exception:
                        continue
            except Exception:
                log.debug("Failed to scan service type %s", stid, exc_info=True)
                continue

        return best

    async def _discover_and_poll(self) -> LiveStatus:
        """Find the right plan by scanning all service types in the folder."""
        # Try to find a live plan across all service types
        best = await self._find_best_live_plan()

        if best:
            plan_id, st_id, live_start_at, service_start, planned_end = best
            self._locked_service_start = service_start
            self._locked_planned_end = planned_end
            self._locked_live_start_at = live_start_at
            status = await self._poll_live(plan_id, st_id)
            if status.is_live or status.finished:
                self._locked_plan_id = plan_id
                self._locked_st_id = st_id
                import time
                self._last_full_scan = time.time()
                log.info("Locked onto live plan %s (st=%s), service_start=%s",
                         plan_id, st_id, service_start)
                return status
            # Clear tentative values
            self._locked_service_start = None
            self._locked_planned_end = None
            self._locked_live_start_at = None

        # Nothing live — find next upcoming plan for "starts in" display
        return await self._find_upcoming_plan()

    async def _find_upcoming_plan(self) -> LiveStatus:
        """Find the next upcoming plan across all service types for fallback display."""
        client = await self._get_client()
        st_ids = await self._get_service_type_ids()

        nearest_plan = None
        nearest_start = None

        for stid in st_ids:
            plan, plan_times = await self._fetch_plan(client, stid, "future", "sort_date")
            if plan:
                service_start, _ = self._extract_service_times(plan_times)
                if service_start is None:
                    sort_date = plan["attributes"].get("sort_date")
                    if sort_date:
                        service_start = dateutil.parser.isoparse(sort_date)

                if service_start:
                    if nearest_start is None or service_start < nearest_start:
                        nearest_start = service_start
                        nearest_plan = plan

            # Also check past plan (may be currently running but Live not started)
            past_plan, past_plan_times = await self._fetch_plan(client, stid, "past", "-sort_date")
            if past_plan:
                sort_date_str = past_plan["attributes"].get("sort_date")
                if sort_date_str:
                    plan_date = dateutil.parser.isoparse(sort_date_str)
                    age = (datetime.now(timezone.utc) - plan_date).total_seconds()
                    if age < _PAST_PLAN_MAX_AGE:
                        title = past_plan["attributes"].get("title") or past_plan["attributes"].get("dates", "")
                        # Past plan exists but not live — show it as fallback
                        past_start, _ = self._extract_service_times(past_plan_times)
                        if past_start and (nearest_start is None or past_start > nearest_start):
                            nearest_start = past_start
                            nearest_plan = past_plan

        if nearest_plan:
            return self._upcoming_status(nearest_plan, service_start=nearest_start)

        return LiveStatus(message="No upcoming plans")

    async def _fetch_plan(
        self, client: httpx.AsyncClient, service_type_id: str, filter_: str, order: str,
    ) -> tuple[dict | None, list[dict]]:
        """Fetch one plan from the PCO API, including plan_times."""
        try:
            resp = await client.get(
                f"{BASE_URL}/service_types/{service_type_id}/plans",
                params={
                    "filter": filter_,
                    "order": order,
                    "per_page": "3",
                    "include": "plan_times",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            plan = data["data"][0] if data.get("data") else None
            plan_times = [
                o for o in data.get("included", [])
                if o.get("type") == "PlanTime"
            ]
            return plan, plan_times
        except Exception:
            log.debug("Failed to fetch %s plan", filter_, exc_info=True)
            return None, []

    @staticmethod
    def _extract_service_times(plan_times: list[dict]) -> tuple[datetime | None, datetime | None]:
        """Find service start and end times from PlanTime objects.

        Looks for the first PlanTime with time_type == "service" and
        returns its (starts_at, ends_at) datetimes.
        """
        for pt in plan_times:
            attrs = pt.get("attributes", {})
            if attrs.get("time_type") == "service":
                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                start = dateutil.parser.isoparse(starts_at) if starts_at else None
                end = dateutil.parser.isoparse(ends_at) if ends_at else None
                if start is not None:
                    return start, end
        return None, None

    @staticmethod
    def _upcoming_status(plan: dict, service_start: datetime | None = None) -> LiveStatus:
        """Build a 'not yet live' status from a future plan."""
        title = plan["attributes"].get("title") or plan["attributes"].get("dates", "")

        # Prefer PlanTime starts_at (accurate), fall back to sort_date
        if service_start is None:
            sort_date_str = plan["attributes"].get("sort_date")
            if sort_date_str:
                service_start = dateutil.parser.isoparse(sort_date_str)

        if service_start is None:
            return LiveStatus(message=f"Next: {title}", plan_title=title)

        now = datetime.now(timezone.utc)
        if now >= service_start:
            # Start time passed but Live not started yet
            return LiveStatus(message="Not live", plan_title=title)

        delta_s = (service_start - now).total_seconds()
        if delta_s > 3600:
            return LiveStatus(message=f"Next: {title}", plan_title=title)
        return LiveStatus(
            message=f"Starts in {int(delta_s // 60)}m",
            plan_title=title,
        )

    # ── Live controller polling ──────────────────────────────────────────

    async def _poll_live(self, plan_id: str, service_type_id: str | None = None) -> LiveStatus:
        """Poll the Live controller endpoint for a specific plan.

        GET /service_types/{id}/plans/{id}/live?include=items,current_item_time

        Returns real-time data: which item is live, when it started,
        who is controlling the session.
        """
        stid = service_type_id or self._locked_st_id or self._service_type_id
        if not stid:
            return LiveStatus(message="No service type for plan")

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BASE_URL}/service_types/{stid}/plans/{plan_id}/live",
                params={"include": "items,current_item_time"},
            )
            if resp.status_code == 404:
                return LiveStatus(message="Plan not found")
            resp.raise_for_status()
        except Exception:
            log.debug("Live endpoint request failed", exc_info=True)
            return LiveStatus(message="Live API error")

        result = resp.json()
        return self._parse_live_response(
            result, service_start=self._locked_service_start, planned_end=self._locked_planned_end,
        )

    def _parse_live_response(
        self, result: dict, service_start: datetime | None = None,
        planned_end: datetime | None = None,
    ) -> LiveStatus:
        """Parse the JSON response from the Live controller endpoint."""
        data = result.get("data", {})
        attrs = data.get("attributes", {})
        plan_title = attrs.get("title", "") or ""
        included = result.get("included", [])

        # Check for current item time (the reliable signal)
        rels = data.get("relationships", {})
        current_item_time_ref = rels.get("current_item_time", {}).get("data")

        # ── Not live ────────────────────────────────────────────────
        if not current_item_time_ref:
            if self._seen_active_item:
                # Had active items before → service finished
                items_only = [
                    o for o in included
                    if o.get("type") == "Item"
                    and o.get("attributes", {}).get("item_type") != "header"
                    and o.get("attributes", {}).get("service_position") == "during"
                ]
                return self._finished_status(plan_title, attrs, items_only, planned_end)
            return LiveStatus(is_live=False, plan_title=plan_title, message="Not live")

        # Separate Item objects from ItemTime objects in included.
        # Filter to "during" service_position only — excludes pre/post-service items.
        items_only = [
            o for o in included
            if o.get("type") == "Item"
            and o.get("attributes", {}).get("item_type") != "header"
            and o.get("attributes", {}).get("service_position") == "during"
        ]

        # Prefer PlanTime ends_at. Fall back to service_start + sum(during items).
        if planned_end is not None:
            planned_service_end = planned_end
        elif service_start is not None:
            total_items_length = sum(i["attributes"].get("length", 0) for i in items_only)
            planned_service_end = service_start + timedelta(seconds=total_items_length)
        else:
            planned_service_end = None

        # ── Find the current ItemTime and its associated Item ───────
        current_item_time_id = current_item_time_ref.get("id")
        current_item_time_obj = next(
            (o for o in included if o.get("type") == "ItemTime" and o.get("id") == current_item_time_id),
            None,
        )

        if not current_item_time_obj:
            return LiveStatus(is_live=True, plan_title=plan_title, message="Live")

        # Resolve the Item this ItemTime belongs to
        item_data = (
            current_item_time_obj.get("relationships", {})
            .get("item", {})
            .get("data")
        )
        current_item_id = item_data.get("id") if isinstance(item_data, dict) else None
        current_item = next(
            (o for o in included if o.get("id") == current_item_id),
            None,
        ) if current_item_id else None

        # ── END OF SERVICE: current resolves to a non-Item ──────────
        if current_item and current_item.get("type") != "Item":
            return self._finished_status(plan_title, attrs, items_only, planned_service_end)

        # ── Pre-service countdown: ItemTime exists but no Item ──────
        if not current_item:
            return LiveStatus(is_live=True, plan_title=plan_title, message="Pre-service")

        # ── Active item ─────────────────────────────────────────────
        self._seen_active_item = True
        return self._active_item_status(
            plan_title, current_item, current_item_time_obj, items_only, planned_service_end,
        )

    def _finished_status(
        self, plan_title: str, plan_attrs: dict, items: list[dict],
        planned_service_end: datetime | None,
    ) -> LiveStatus:
        """Build status for END OF SERVICE (operator advanced past last item)."""
        total_length = sum(i["attributes"].get("length", 0) for i in items)

        service_end_time = None
        live_start_str = plan_attrs.get("live_start_at")
        if live_start_str:
            service_start = dateutil.parser.isoparse(live_start_str)
            service_end_time = service_start + timedelta(seconds=total_length)

        return LiveStatus(
            is_live=True,
            finished=True,
            plan_title=plan_title,
            service_end_time=service_end_time,
            planned_service_end=planned_service_end,
        )

    @staticmethod
    def _active_item_status(
        plan_title: str,
        current_item: dict,
        current_item_time_obj: dict,
        items: list[dict],
        planned_service_end: datetime | None,
    ) -> LiveStatus:
        """Build status for a currently-active item."""
        item_attrs = current_item["attributes"]
        item_title = item_attrs.get("title", "")
        item_description = item_attrs.get("description", "") or ""
        item_length = item_attrs.get("length", 0)
        service_position = item_attrs.get("service_position", "during")

        # ── Pre-service item: service hasn't started yet ──────────
        if service_position == "pre":
            total_during = sum(i["attributes"].get("length", 0) for i in items)
            first_during = items[0]["attributes"].get("title", "") if items else ""
            return LiveStatus(
                is_live=True,
                plan_title=plan_title,
                item_title=item_title,
                item_description=item_description,
                item_end_time=None,
                service_end_time=planned_service_end,
                remaining_items_length=total_during,
                next_item_title=first_during,
                plan_index=0,
                plan_length=len(items),
                planned_service_end=planned_service_end,
            )

        # ── Post-service item: service is over ────────────────────
        if service_position == "post":
            return LiveStatus(
                is_live=True,
                plan_title=plan_title,
                item_title=item_title,
                item_description=item_description,
                item_end_time=None,
                service_end_time=None,
                remaining_items_length=0,
                next_item_title="",
                plan_index=len(items),
                plan_length=len(items),
                planned_service_end=planned_service_end,
            )

        # ── During-service item ───────────────────────────────────
        # When did this item actually start?
        live_start_str = current_item_time_obj["attributes"].get("live_start_at")
        item_end_time = None
        if live_start_str and item_length:
            item_started = dateutil.parser.isoparse(live_start_str)
            item_end_time = item_started + timedelta(seconds=item_length)

        # Find position in service and sum remaining items
        current_item_id = current_item["id"]
        plan_index = 0
        remaining_length = 0
        found_current = False
        next_item_title = ""

        for i, item in enumerate(items):
            if item["id"] == current_item_id:
                plan_index = i + 1
                found_current = True
                continue
            if found_current:
                remaining_length += item["attributes"].get("length", 0)
                if not next_item_title:
                    next_item_title = item["attributes"].get("title", "")

        # service_end = whichever is later (planned item end or now) + remaining
        # This makes service_end extend in real time when an item runs over.
        now = datetime.now(timezone.utc)
        service_end_time = None
        if item_end_time is not None:
            anchor = max(item_end_time, now)
            service_end_time = anchor + timedelta(seconds=remaining_length)

        return LiveStatus(
            is_live=True,
            plan_title=plan_title,
            item_title=item_title,
            item_description=item_description,
            item_end_time=item_end_time,
            service_end_time=service_end_time,
            remaining_items_length=remaining_length,
            next_item_title=next_item_title,
            plan_index=plan_index,
            plan_length=len(items),
            planned_service_end=planned_service_end,
        )
