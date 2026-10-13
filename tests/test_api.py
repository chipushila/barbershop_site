"""Integration tests. Require a THROWAWAY PostgreSQL database:

    TEST_DATABASE_URL=postgresql://user:pass@localhost/air_test pytest -q

Never point this at production: it creates bookings.
"""
import os
import uuid
from datetime import timedelta

import pytest

url = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not url, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = url
    import backend
    backend.app.testing = True
    return backend.app.test_client(), backend


def first_slot(c, barber=1, svc=2):
    cfg = c.get("/api/config").get_json()
    for d in cfg["dates"]:
        r = c.get(f"/api/availability?barber_id={barber}&date={d['date']}&service_ids={svc}").get_json()
        for s in r["slots"]:
            if s["available"]:
                return d["date"], s["time"]
    pytest.skip("no free slot")


def payload(date, time, barber=1, svc=2, **kw):
    p = dict(first_name="Test", last_name="User", phone="+77010000000", barber_id=barber,
             service_ids=[svc], date=date, time=time, idempotency_key=uuid.uuid4().hex)
    p.update(kw)
    return p


def test_homepage_and_static(client):
    c, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/static/script.js").status_code == 200


def test_health_and_timezone(client):
    c, _ = client
    assert c.get("/healthz").get_json()["ok"] is True
    assert c.get("/api/config").get_json()["timezone"] in ("Asia/Astana", "Asia/Almaty")


def test_booking_roundtrip_and_duplicate(client):
    c, _ = client
    date, time = first_slot(c)
    r = c.post("/api/book", json=payload(date, time))
    assert r.status_code == 201
    bid = r.get_json()["booking"]["booking_id"]
    got = c.get(f"/api/bookings/{bid}?phone=%2B77010000000")
    assert got.status_code == 200
    # same slot again (different customer) must be rejected
    dup = c.post("/api/book", json=payload(date, time, barber=2))
    assert dup.status_code == 409  # shop-wide blocking
    c.post("/api/cancel", json={"appointment_id": bid, "phone": "+77010000000"})


def test_idempotent_retry(client):
    c, _ = client
    date, time = first_slot(c)
    p = payload(date, time)
    a, b = c.post("/api/book", json=p), c.post("/api/book", json=p)
    assert a.status_code == 201 and b.status_code == 200
    assert a.get_json()["appointment_id"] == b.get_json()["appointment_id"]
    c.post("/api/cancel", json={"appointment_id": a.get_json()["appointment_id"], "phone": "+77010000000"})


@pytest.mark.parametrize("bad", [
    {"first_name": ""}, {"phone": "123"}, {"service_ids": []}, {"time": "99:99"},
    {"date": "2000-01-01"}, {"barber_id": 99999},
])
def test_invalid_submissions_rejected(client, bad):
    c, _ = client
    date, time = first_slot(c)
    assert c.post("/api/book", json=payload(date, time, **bad)).status_code >= 400


def test_no_secrets_exposed(client):
    c, _ = client
    body = c.get("/").get_data(as_text=True) + c.get("/static/script.js").get_data(as_text=True)
    assert "DATABASE_URL" not in body and "ADMIN_TOKEN" not in body
    assert c.get("/api/admin/schedule").status_code in (401, 503)
