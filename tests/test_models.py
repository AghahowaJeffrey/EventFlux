"""
Unit tests for shared Pydantic models — no I/O required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from shared.models import BulkEventRequest, Event


def valid_event(**overrides) -> dict:
    base = {
        "event_type": "user_signup",
        "actor_id": "user_001",
        "source": "web",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attributes": {"plan": "pro"},
    }
    base.update(overrides)
    return base


class TestEvent:
    def test_valid_event(self):
        e = Event(**valid_event())
        assert e.event_type == "user_signup"

    def test_missing_required_field_raises(self):
        data = valid_event()
        del data["actor_id"]
        with pytest.raises(ValidationError) as exc_info:
            Event(**data)
        assert "actor_id" in str(exc_info.value)

    def test_whitespace_stripped_from_strings(self):
        e = Event(**valid_event(event_type="  page_view  ", source="  api  "))
        assert e.event_type == "page_view"
        assert e.source == "api"

    def test_empty_event_type_raises(self):
        with pytest.raises(ValidationError):
            Event(**valid_event(event_type=""))

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            Event(**valid_event(unknown_field="x"))

    def test_attributes_defaults_to_empty_dict(self):
        data = valid_event()
        del data["attributes"]
        e = Event(**data)
        assert e.attributes == {}

    def test_event_type_max_length(self):
        with pytest.raises(ValidationError):
            Event(**valid_event(event_type="x" * 129))


class TestBulkEventRequest:
    def test_valid_bulk_request(self):
        payload = {"events": [valid_event()]}
        req = BulkEventRequest(**payload)
        assert len(req.events) == 1

    def test_request_id_auto_generated(self):
        req = BulkEventRequest(events=[valid_event()])
        assert isinstance(req.request_id, UUID)

    def test_request_id_can_be_provided(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        req = BulkEventRequest(request_id=uid, events=[valid_event()])
        assert str(req.request_id) == uid

    def test_empty_events_list_raises(self):
        with pytest.raises(ValidationError):
            BulkEventRequest(events=[])

    def test_multiple_events(self):
        req = BulkEventRequest(events=[valid_event() for _ in range(10)])
        assert len(req.events) == 10
