"""Tests for PCO client."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from pi_decoder.pco_client import LiveStatus, PCOClient
from pi_decoder.config import Config


class TestLiveStatus:
    """Test LiveStatus dataclass."""

    def test_default_values(self):
        status = LiveStatus()
        assert status.is_live is False
        assert status.finished is False
        assert status.plan_title == ""
        assert status.item_title == ""
        assert status.item_description == ""
        assert status.item_end_time is None
        assert status.service_end_time is None
        assert status.remaining_items_length == 0.0
        assert status.message == ""

    def test_custom_values(self):
        now = datetime.now(timezone.utc)
        status = LiveStatus(
            is_live=True,
            plan_title="Sunday Service",
            item_title="Worship",
            item_end_time=now,
            message="Live",
        )
        assert status.is_live is True
        assert status.plan_title == "Sunday Service"
        assert status.item_title == "Worship"
        assert status.item_end_time == now


class TestPCOClient:
    """Test PCOClient functionality."""

    @pytest.fixture
    def folder_config(self):
        cfg = Config()
        cfg.pco.app_id = "test_app"
        cfg.pco.secret = "test_secret"
        cfg.pco.folder_id = "000000"
        cfg.pco.search_mode = "folder"
        return cfg

    def test_init(self, pco_config):
        client = PCOClient(pco_config)
        assert client._service_type_id == "12345"
        assert client._auth == ("test_app", "test_secret")
        assert client._locked_service_start is None
        assert client._locked_planned_end is None
        assert client._seen_active_item is False

    def test_init_with_folder(self, folder_config):
        client = PCOClient(folder_config)
        assert client._folder_id == "000000"
        assert client._service_type_id == ""

    def test_update_credentials(self, pco_config):
        client = PCOClient(pco_config)
        client.update_credentials("new_app", "new_secret", "67890")

        assert client._auth == ("new_app", "new_secret")
        assert client._service_type_id == "67890"
        assert client._locked_plan_id is None
        assert client._locked_st_id is None
        assert client._locked_service_start is None
        assert client._locked_planned_end is None
        assert client._locked_live_start_at is None
        assert client._seen_active_item is False

    def test_cached_status(self, pco_config):
        client = PCOClient(pco_config)
        # Initial cached status
        assert client.cached_status.message == "Initializing..."

    def test_no_service_type_or_folder_returns_error(self):
        """Neither folder_id nor service_type_id configured."""
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.service_type_id = ""
        cfg.pco.folder_id = ""
        client = PCOClient(cfg)

        import asyncio
        status = asyncio.get_event_loop().run_until_complete(
            client.get_live_status()
        )
        assert "No service type configured" in status.message

    def test_folder_id_allows_get_live_status(self, folder_config):
        """With folder_id set, get_live_status doesn't return 'not configured'."""
        client = PCOClient(folder_config)
        # The folder_id is set, so it should try to fetch (and fail with connection error,
        # not the "not configured" message)
        assert client._folder_id == "000000"


class TestParseLiveResponse:
    """Test parsing of PCO Live API responses."""

    @pytest.fixture
    def client(self, pco_config):
        return PCOClient(pco_config)

    def test_parse_not_live_no_controller(self, client):
        """No current_item_time and _seen_active_item=False → Not live."""
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {},
                "relationships": {},
            },
            "included": [],
        }
        status = client._parse_live_response(response)
        assert status.is_live is False
        assert status.plan_title == "Sunday Service"
        assert status.message == "Not live"

    def test_parse_not_live_with_controller_link(self, client):
        """Controller link present but no current_item_time → Not live (controller is ignored)."""
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {},
            },
            "included": [],
        }
        # Controller link should NOT be used for live detection.
        # With _seen_active_item=False, this is "Not live".
        status = client._parse_live_response(response)
        assert status.is_live is False
        assert status.message == "Not live"

    def test_parse_finished_with_controller_seen_active(self, client):
        """Controller link + no current_item_time + _seen_active_item → finished."""
        client._seen_active_item = True
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {},
            },
            "included": [
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(response)
        assert status.is_live is True
        assert status.finished is True

    def test_parse_live_with_item(self, client):
        """Test parsing with an active item."""
        now = datetime.now(timezone.utc)
        service_start = now - timedelta(minutes=10)
        response = {
            "data": {
                "attributes": {
                    "title": "Sunday Service",
                },
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": (now - timedelta(minutes=5)).isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-1"}}
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "description": "Opening songs",
                        "length": 900,  # 15 minutes
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(response, service_start=service_start)
        assert status.is_live is True
        assert status.item_title == "Worship"
        assert status.item_description == "Opening songs"
        assert status.plan_title == "Sunday Service"
        # planned_service_end should be computed from service_start
        assert status.planned_service_end == service_start + timedelta(seconds=900)

    def test_parse_filters_during_items_only(self, client):
        """Verify pre-service items are excluded from totals."""
        now = datetime.now(timezone.utc)
        service_start = now - timedelta(minutes=5)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": (now - timedelta(minutes=2)).isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-during"}}
                    },
                },
                # Pre-service item — should be excluded
                {
                    "id": "item-pre",
                    "type": "Item",
                    "attributes": {
                        "title": "Sound Check",
                        "length": 600,
                        "item_type": "item",
                        "service_position": "pre",
                    },
                },
                # During-service item (current)
                {
                    "id": "item-during",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
                # Post-service item — should be excluded
                {
                    "id": "item-post",
                    "type": "Item",
                    "attributes": {
                        "title": "Cleanup",
                        "length": 300,
                        "item_type": "item",
                        "service_position": "post",
                    },
                },
                # Header item — should be excluded
                {
                    "id": "item-header",
                    "type": "Item",
                    "attributes": {
                        "title": "Section Header",
                        "length": 0,
                        "item_type": "header",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(response, service_start=service_start)
        # Only the "during" non-header item (900s) should be counted
        assert status.planned_service_end == service_start + timedelta(seconds=900)
        assert status.plan_length == 1  # only 1 during item

    def test_parse_pre_service_countdown(self, client):
        """current_item_time with null item relationship → Pre-service."""
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "relationships": {
                        # item relationship is null — PlanTime-level countdown
                        "item": {"data": None}
                    },
                },
            ],
        }
        status = client._parse_live_response(response)
        assert status.is_live is True
        assert status.message == "Pre-service"

    def test_finished_state_detection(self, client):
        """_seen_active_item + no current_item_time → finished."""
        # Simulate having seen active items before
        client._seen_active_item = True
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {},
            },
            "included": [
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(response)
        assert status.is_live is True
        assert status.finished is True

    def test_planned_service_end_prefers_planned_end(self, client):
        """planned_service_end uses PlanTime ends_at when available."""
        now = datetime.now(timezone.utc)
        service_start = now - timedelta(minutes=10)
        planned_end = now + timedelta(minutes=50)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": (now - timedelta(minutes=5)).isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-1"}}
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(
            response, service_start=service_start, planned_end=planned_end,
        )
        # Should use planned_end, not service_start + items sum
        assert status.planned_service_end == planned_end

    def test_planned_service_end_fallback_to_items_sum(self, client):
        """planned_service_end falls back to service_start + items when no planned_end."""
        now = datetime.now(timezone.utc)
        service_start = now - timedelta(minutes=10)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": (now - timedelta(minutes=5)).isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-1"}}
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
                {
                    "id": "item-2",
                    "type": "Item",
                    "attributes": {
                        "title": "Sermon",
                        "length": 1800,
                        "item_type": "item",
                        "service_position": "during",
                    },
                },
            ],
        }
        # No planned_end → falls back to service_start + sum(during items)
        status = client._parse_live_response(response, service_start=service_start)
        # 900 + 1800 = 2700 seconds total
        assert status.planned_service_end == service_start + timedelta(seconds=2700)

    def test_planned_service_end_none_without_service_start(self, client):
        """planned_service_end is None when no service_start provided."""
        now = datetime.now(timezone.utc)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": now.isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-1"}}
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        # No service_start → planned_service_end should be None
        status = client._parse_live_response(response, service_start=None)
        assert status.planned_service_end is None

    def test_pre_service_item_gives_full_remaining(self, client):
        """Current item is pre-service → remaining = all during items, item_end_time = None."""
        now = datetime.now(timezone.utc)
        planned_end = now + timedelta(minutes=60)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": now.isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-pre"}}
                    },
                },
                {
                    "id": "item-pre",
                    "type": "Item",
                    "attributes": {
                        "title": "Sound Check",
                        "length": 600,
                        "item_type": "item",
                        "service_position": "pre",
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
                {
                    "id": "item-2",
                    "type": "Item",
                    "attributes": {
                        "title": "Sermon",
                        "length": 1800,
                        "item_type": "item",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(
            response, service_start=now - timedelta(minutes=5), planned_end=planned_end,
        )
        assert status.is_live is True
        assert status.item_title == "Sound Check"
        assert status.item_end_time is None
        assert status.remaining_items_length == 2700  # 900 + 1800
        assert status.service_end_time == planned_end
        assert status.next_item_title == "Worship"
        assert status.plan_index == 0
        assert status.plan_length == 2

    def test_post_service_item_gives_zero_remaining(self, client):
        """Current item is post-service → remaining = 0, item_end_time = None."""
        now = datetime.now(timezone.utc)
        response = {
            "data": {
                "attributes": {"title": "Sunday Service"},
                "links": {"controller": "/users/123"},
                "relationships": {
                    "current_item_time": {
                        "data": {"id": "item-time-1", "type": "ItemTime"}
                    }
                },
            },
            "included": [
                {
                    "id": "item-time-1",
                    "type": "ItemTime",
                    "attributes": {
                        "live_start_at": now.isoformat(),
                    },
                    "relationships": {
                        "item": {"data": {"id": "item-post"}}
                    },
                },
                {
                    "id": "item-post",
                    "type": "Item",
                    "attributes": {
                        "title": "Cleanup",
                        "length": 300,
                        "item_type": "item",
                        "service_position": "post",
                    },
                },
                {
                    "id": "item-1",
                    "type": "Item",
                    "attributes": {
                        "title": "Worship",
                        "length": 900,
                        "item_type": "song",
                        "service_position": "during",
                    },
                },
            ],
        }
        status = client._parse_live_response(response)
        assert status.is_live is True
        assert status.item_title == "Cleanup"
        assert status.item_end_time is None
        assert status.remaining_items_length == 0
        assert status.service_end_time is None
        assert status.next_item_title == ""
        assert status.plan_index == 1  # len(during items)
        assert status.plan_length == 1


class TestExtractServiceTimes:
    """Test PlanTime parsing for service start and end."""

    def test_extract_service_times(self):
        plan_times = [
            {
                "type": "PlanTime",
                "attributes": {
                    "time_type": "rehearsal",
                    "starts_at": "2025-01-12T08:00:00Z",
                },
            },
            {
                "type": "PlanTime",
                "attributes": {
                    "time_type": "service",
                    "starts_at": "2025-01-12T09:30:00Z",
                    "ends_at": "2025-01-12T11:00:00Z",
                },
            },
        ]
        start, end = PCOClient._extract_service_times(plan_times)
        assert start == datetime(2025, 1, 12, 9, 30, tzinfo=timezone.utc)
        assert end == datetime(2025, 1, 12, 11, 0, tzinfo=timezone.utc)

    def test_extract_service_times_no_ends_at(self):
        """PlanTime without ends_at returns (start, None)."""
        plan_times = [
            {
                "type": "PlanTime",
                "attributes": {
                    "time_type": "service",
                    "starts_at": "2025-01-12T09:30:00Z",
                },
            },
        ]
        start, end = PCOClient._extract_service_times(plan_times)
        assert start == datetime(2025, 1, 12, 9, 30, tzinfo=timezone.utc)
        assert end is None

    def test_extract_service_times_no_service_time(self):
        plan_times = [
            {
                "type": "PlanTime",
                "attributes": {
                    "time_type": "rehearsal",
                    "starts_at": "2025-01-12T08:00:00Z",
                },
            },
        ]
        start, end = PCOClient._extract_service_times(plan_times)
        assert start is None
        assert end is None

    def test_extract_service_times_empty(self):
        start, end = PCOClient._extract_service_times([])
        assert start is None
        assert end is None


class TestUpcomingStatus:
    """Test upcoming plan status generation."""

    def test_future_plan_shows_next(self):
        now = datetime.now(timezone.utc)
        plan = {
            "attributes": {
                "title": "Next Sunday",
                "sort_date": (now + timedelta(days=1)).isoformat(),
            }
        }
        status = PCOClient._upcoming_status(plan)
        assert "Next: Next Sunday" in status.message

    def test_soon_plan_shows_countdown(self):
        now = datetime.now(timezone.utc)
        plan = {
            "attributes": {
                "title": "Soon Service",
                "sort_date": (now + timedelta(minutes=30)).isoformat(),
            }
        }
        status = PCOClient._upcoming_status(plan)
        assert "Starts in" in status.message

    def test_soon_plan_uses_service_start_when_provided(self):
        """Verify service_start (PlanTime) is used over sort_date."""
        now = datetime.now(timezone.utc)
        # sort_date is 2 hours away (would show "Next:")
        plan = {
            "attributes": {
                "title": "Soon Service",
                "sort_date": (now + timedelta(hours=2)).isoformat(),
            }
        }
        # But actual service_start is 30 minutes away
        service_start = now + timedelta(minutes=30)
        status = PCOClient._upcoming_status(plan, service_start=service_start)
        assert "Starts in" in status.message

    def test_past_plan_not_live(self):
        now = datetime.now(timezone.utc)
        plan = {
            "attributes": {
                "title": "Past Service",
                "sort_date": (now - timedelta(minutes=10)).isoformat(),
            }
        }
        status = PCOClient._upcoming_status(plan)
        assert status.message == "Not live"


class TestTestConnection:
    """Test the test_connection() method with mocked HTTP."""

    @pytest.fixture
    def client(self, pco_config):
        return PCOClient(pco_config)

    @pytest.mark.asyncio
    async def test_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "1", "attributes": {"name": "Sunday", "frequency": "Weekly"}},
            ]
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.test_connection()
        assert result["success"] is True
        assert len(result["service_types"]) == 1
        assert result["service_types"][0]["name"] == "Sunday"

    @pytest.mark.asyncio
    async def test_401_auth_failure(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.test_connection()
        assert result["success"] is False
        assert "401" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout(self, client):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.test_connection()
        assert result["success"] is False
        assert "timeout" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_connect_error(self, client):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.test_connection()
        assert result["success"] is False
        assert "Connection error" in result["error"]

    @pytest.mark.asyncio
    async def test_http_500_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)
        )
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.test_connection()
        assert result["success"] is False


class TestGetServiceTypes:
    """Test get_service_types() delegates to test_connection."""

    @pytest.mark.asyncio
    async def test_returns_service_types(self, pco_config):
        client = PCOClient(pco_config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "1", "attributes": {"name": "Sunday", "frequency": "Weekly"}},
                {"id": "2", "attributes": {"name": "Wednesday", "frequency": "Weekly"}},
            ]
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        client._client = mock_http

        types = await client.get_service_types()
        assert len(types) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, pco_config):
        client = PCOClient(pco_config)
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_http.is_closed = False
        client._client = mock_http

        types = await client.get_service_types()
        assert types == []


class TestServiceTypeDiscovery:
    """Test _get_service_type_ids() for folder vs service_type mode."""

    @pytest.mark.asyncio
    async def test_service_type_mode_returns_single_id(self, pco_config):
        client = PCOClient(pco_config)
        ids = await client._get_service_type_ids()
        assert ids == ["12345"]

    @pytest.mark.asyncio
    async def test_folder_mode_fetches_from_api(self):
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.folder_id = "999"
        cfg.pco.search_mode = "folder"
        client = PCOClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"id": "10"}, {"id": "20"}]
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.is_closed = False
        client._client = mock_http

        ids = await client._get_service_type_ids()
        assert ids == ["10", "20"]

    @pytest.mark.asyncio
    async def test_folder_mode_returns_empty_on_error(self):
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.folder_id = "999"
        cfg.pco.search_mode = "folder"
        client = PCOClient(cfg)

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("network error"))
        mock_http.is_closed = False
        client._client = mock_http

        ids = await client._get_service_type_ids()
        assert ids == []

    @pytest.mark.asyncio
    async def test_no_config_returns_empty(self):
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.service_type_id = ""
        cfg.pco.folder_id = ""
        cfg.pco.search_mode = "service_type"
        client = PCOClient(cfg)

        ids = await client._get_service_type_ids()
        assert ids == []


class TestGetLiveStatus:
    """Test get_live_status() error handling."""

    @pytest.mark.asyncio
    async def test_no_folder_configured(self):
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.folder_id = ""
        cfg.pco.search_mode = "folder"
        client = PCOClient(cfg)

        status = await client.get_live_status()
        assert "No folder ID" in status.message

    @pytest.mark.asyncio
    async def test_no_service_type_configured(self):
        cfg = Config()
        cfg.pco.app_id = "test"
        cfg.pco.secret = "test"
        cfg.pco.service_type_id = ""
        cfg.pco.search_mode = "service_type"
        client = PCOClient(cfg)

        status = await client.get_live_status()
        assert "No service type" in status.message

    @pytest.mark.asyncio
    async def test_returns_cached_on_http_error(self, pco_config):
        client = PCOClient(pco_config)
        client._cached_status = LiveStatus(message="cached value")

        with patch.object(client, "_fetch_live_status", side_effect=httpx.TimeoutException("timeout")):
            status = await client.get_live_status()
        assert status.message == "cached value"

    @pytest.mark.asyncio
    async def test_returns_cached_on_parse_error(self, pco_config):
        client = PCOClient(pco_config)
        client._cached_status = LiveStatus(message="cached value")

        with patch.object(client, "_fetch_live_status", side_effect=KeyError("missing")):
            status = await client.get_live_status()
        assert status.message == "cached value"


class TestClose:
    """Test client cleanup."""

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self, pco_config):
        client = PCOClient(pco_config)
        mock_http = AsyncMock()
        mock_http.is_closed = False
        mock_http.aclose = AsyncMock()
        client._client = mock_http

        await client.close()
        mock_http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_noop_when_no_client(self, pco_config):
        client = PCOClient(pco_config)
        client._client = None
        # Should not raise
        await client.close()

    @pytest.mark.asyncio
    async def test_close_noop_when_already_closed(self, pco_config):
        client = PCOClient(pco_config)
        mock_http = AsyncMock()
        mock_http.is_closed = True
        client._client = mock_http
        # Should not call aclose
        await client.close()


class TestCircuitBreaker:
    """Test circuit breaker: exponential backoff after consecutive failures."""

    def test_record_failure_increments_count(self, pco_config):
        client = PCOClient(pco_config)
        assert client._consecutive_failures == 0
        client._record_failure()
        assert client._consecutive_failures == 1
        # Below threshold — no backoff
        assert client._backoff_until == 0.0

    def test_circuit_breaker_activates_at_threshold(self, pco_config):
        import time
        client = PCOClient(pco_config)
        for _ in range(5):
            client._record_failure()
        assert client._consecutive_failures == 5
        assert client._backoff_until > time.monotonic() - 1

    def test_exponential_backoff_scales(self, pco_config):
        import time
        client = PCOClient(pco_config)
        client._consecutive_failures = 6  # already past threshold
        client._record_failure()  # now 7 failures
        # backoff = min(2^(7-5), 300) = min(4, 300) = 4
        expected_backoff = 4
        assert client._backoff_until == pytest.approx(time.monotonic() + expected_backoff, abs=1.0)

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_poll(self, pco_config):
        import time
        client = PCOClient(pco_config)
        client._consecutive_failures = 5
        client._backoff_until = time.monotonic() + 300  # far in the future
        client._cached_status = LiveStatus(message="cached during backoff")

        status = await client.get_live_status()
        assert status.message == "cached during backoff"

    def test_update_credentials_resets_circuit_breaker(self, pco_config):
        client = PCOClient(pco_config)
        client._consecutive_failures = 10
        client._backoff_until = 99999.0
        client.update_credentials("new", "new", "1")
        assert client._consecutive_failures == 0
        assert client._backoff_until == 0.0


class TestUpdateCredentials:
    """Test credential update with folder/search mode."""

    def test_update_with_folder_id(self, pco_config):
        client = PCOClient(pco_config)
        client.update_credentials("new_app", "new_secret", "99", folder_id="folder1", search_mode="folder")
        assert client._auth == ("new_app", "new_secret")
        assert client._service_type_id == "99"
        assert client._folder_id == "folder1"
        assert client._search_mode == "folder"

    def test_update_resets_lock_state(self, pco_config):
        client = PCOClient(pco_config)
        client._locked_plan_id = "plan1"
        client._locked_st_id = "st1"
        client._seen_active_item = True
        client.update_credentials("new", "new", "1")
        assert client._locked_plan_id is None
        assert client._locked_st_id is None
        assert client._seen_active_item is False

    def test_update_invalidates_http_client(self, pco_config):
        client = PCOClient(pco_config)
        mock_http = MagicMock()
        mock_http.is_closed = False
        client._client = mock_http
        client.update_credentials("new", "new", "1")
        assert client._client is None
