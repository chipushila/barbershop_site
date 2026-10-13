import hmac
import logging
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template, request
from psycopg2 import errors

# ---------- Configuration ----------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):  # some hosts still emit the legacy scheme
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

if DATABASE_URL:
    DB_CONFIG = {"dsn": DATABASE_URL}
else:  # local development fallback; set DB_PASSWORD in your environment / .env
    DB_CONFIG = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", 5432)),
        "dbname": os.getenv("DB_NAME", "barbershop_db"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

# Safely load the timezone, instructing Windows users to install tzdata if missing.
try:
    TZ = ZoneInfo(os.getenv("BARBERSHOP_TZ", "Asia/Astana"))
except ZoneInfoNotFoundError:
    sys.exit("CRITICAL ERROR: timezone database not found. Install it with: pip install tzdata")

SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", 15))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", 7))
MAX_SERVICES_PER_BOOKING = 20
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # admin API is disabled if empty
DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# True  = a booked time blocks that time for ALL barbers (shop-wide).
# False = conflicts are evaluated per barber only.
SHOP_WIDE_BLOCKING = os.getenv("SHOP_WIDE_BLOCKING", "1") == "1"
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
SHOP_LOCK_KEY = 7340192

IDEMPOTENCY_INDEX = "uq_appointments_idempotency"
LEGACY_SLOT_INDEX = "uq_barber_slot"
DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
log = logging.getLogger("barbershop")


# ---------- Infrastructure ----------
class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(ok=False, code=e.code, error=e.message, **e.extra), e.status


@app.errorhandler(Exception)
def handle_unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(ok=False, code="http_error", error=e.description), e.code
    log.exception("Unhandled error")
    return jsonify(ok=False, code="server_error", error="Internal server error"), 500


@contextmanager
def db():
    """One connection = one transaction (commit on success, rollback on error)."""
    conn = psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor,
                            connect_timeout=10, **DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_local():
    return datetime.now(TZ)


def local_dt(day, t):
    return datetime.combine(day, t, tzinfo=TZ)


def money(d):
    return f"{Decimal(d):.2f}"


def hhmm(t):
    return t.strftime("%H:%M")


def normalize_phone(raw):
    s = re.sub(r"[\s\-()]", "", str(raw or ""))
    if re.fullmatch(r"8\d{10}", s):
        s = "+7" + s[1:]
    elif re.fullmatch(r"\d{10,15}", s):
        s = "+" + s
    return s


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not ADMIN_TOKEN:
            raise ApiError(503, "admin_disabled", "Admin API is disabled: set ADMIN_TOKEN")
        given = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(given.encode(), ADMIN_TOKEN.encode()):
            raise ApiError(401, "unauthorized", "Admin token required")
        return fn(*a, **kw)
    return wrapper


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "JSON object body required")
    return data


def parse_day(raw):
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ApiError(400, "invalid_date", "Invalid date, expected YYYY-MM-DD")


def parse_hhmm(raw):
    try:
        return datetime.strptime(str(raw), "%H:%M").time()
    except ValueError:
        raise ApiError(400, "invalid_time", "Invalid time, expected HH:MM")


def parse_int(raw, name):
    if isinstance(raw, bool):
        raise ApiError(400, "invalid_" + name, f"Invalid {name}")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_" + name, f"Invalid {name}")


def parse_service_ids(raw):
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "no_services", "Select at least one service")
    if len(raw) > MAX_SERVICES_PER_BOOKING:
        raise ApiError(400, "too_many_services", "Too many services selected")
    ids = [parse_int(v, "service_id") for v in raw]
    if len(set(ids)) != len(ids):
        raise ApiError(400, "duplicate_services", "Duplicate services in selection")
    return ids


def load_services(cur, ids):
    """Current catalog rows for ids; rejects unknown / inactive services."""
    cur.execute(
        "SELECT service_id, service_name, price, duration_minutes, is_active "
        "FROM services WHERE service_id = ANY(%s) ORDER BY service_id", (ids,))
    rows = cur.fetchall()
    found = {r["service_id"] for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ApiError(404, "service_not_found", "Service not found", service_ids=missing)
    inactive = [r["service_id"] for r in rows if not r["is_active"]]
    if inactive:
        raise ApiError(409, "service_inactive", "A selected service is no longer available",
                       service_ids=inactive)
    return rows


def totals(rows):
    """(total_price, total_minutes) computed from current catalog rows."""
    return (sum((Decimal(r["price"]) for r in rows), Decimal("0")),
            sum(int(r["duration_minutes"]) for r in rows))


# ---------- Per-barber schedule logic ----------
class DayContext:
    """Everything that can restrict ONE barber on ONE day."""

    def __init__(self, windows, blocks, bookings):
        self.windows = windows      # [(start, end)] working intervals
        self.blocks = blocks        # [(start, end)] breaks + time off
        self.bookings = bookings    # [(start, end)] non-cancelled bookings


def load_day_context(cur, barber_id, day):
    dow = day.weekday()
    cur.execute("SELECT start_time, end_time FROM barber_working_hours "
                "WHERE barber_id = %s AND day_of_week = %s ORDER BY start_time", (barber_id, dow))
    windows = [(local_dt(day, r["start_time"]), local_dt(day, r["end_time"])) for r in cur.fetchall()]

    cur.execute("SELECT start_time, end_time FROM barber_breaks "
                "WHERE barber_id = %s AND (day_of_week IS NULL OR day_of_week = %s)", (barber_id, dow))
    blocks = [(local_dt(day, r["start_time"]), local_dt(day, r["end_time"])) for r in cur.fetchall()]

    day_start = local_dt(day, time(0, 0))
    day_end = day_start + timedelta(days=1)
    cur.execute("SELECT start_at, end_at FROM barber_time_off "
                "WHERE barber_id = %s AND start_at < %s AND end_at > %s", (barber_id, day_end, day_start))
    blocks += [(r["start_at"], r["end_at"]) for r in cur.fetchall()]

    # Time synchronization: in shop-wide mode a booking by ANY barber occupies
    # that time for everyone; otherwise only this barber's bookings count.
    sql = ("SELECT start_at, end_at FROM appointments "
           "WHERE status <> 'cancelled' AND start_at < %s AND end_at > %s")
    params = [day_end, day_start]
    if not SHOP_WIDE_BLOCKING:
        sql += " AND barber_id = %s"
        params.append(barber_id)
    cur.execute(sql, params)
    bookings = [(r["start_at"], r["end_at"]) for r in cur.fetchall()]
    return DayContext(windows, blocks, bookings)


def interval_problem(ctx, start, end):
    """None if [start, end) is bookable, else a machine-readable reason.
    Overlap rule: existing_start < new_end AND existing_end > new_start."""
    if not any(ws <= start and end <= we for ws, we in ctx.windows):
        return "outside_working_hours"
    if any(bs < end and be > start for bs, be in ctx.blocks):
        return "break_or_time_off"
    if any(bs < end and be > start for bs, be in ctx.bookings):
        return "slot_conflict"
    return None


def compute_slots(ctx, duration_minutes, now):
    dur, step = timedelta(minutes=duration_minutes), timedelta(minutes=SLOT_STEP_MINUTES)
    starts = set()
    for ws, we in ctx.windows:
        t = ws
        while t + dur <= we:
            starts.add(t)
            t += step
    slots = []
    for t in sorted(starts):
        reason = "past" if t <= now else interval_problem(ctx, t, t + dur)
        slots.append({"time": hhmm(t), "end_time": hhmm(t + dur),
                      "available": reason is None, "reason": reason})
    return slots


def get_active_barber(cur, barber_id, lock=False):
    cur.execute("SELECT barber_id, is_active FROM barbers WHERE barber_id = %s"
                + (" FOR UPDATE" if lock else ""), (barber_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "barber_not_found", "Barber not found")
    if not row["is_active"]:
        raise ApiError(409, "barber_inactive", "This barber is not accepting bookings")
    return row


# ---------- Booking serialization ----------
def fetch_booking(cur, appointment_id):
    cur.execute(
        """SELECT a.appointment_id, a.status, a.barber_id, b.first_name AS barber_name,
                  a.customer_first_name, a.customer_last_name, a.customer_phone,
                  a.start_at, a.end_at, a.total_price, a.total_duration_minutes
           FROM appointments a JOIN barbers b ON b.barber_id = a.barber_id
           WHERE a.appointment_id = %s""", (appointment_id,))
    a = cur.fetchone()
    if a is None:
        return None
    cur.execute("""SELECT service_id, service_name_snapshot, price_snapshot, duration_minutes_snapshot
                   FROM appointment_services WHERE appointment_id = %s ORDER BY service_id""",
                (appointment_id,))
    services = [{"service_id": s["service_id"], "name": s["service_name_snapshot"],
                 "price": money(s["price_snapshot"]), "duration_minutes": s["duration_minutes_snapshot"]}
                for s in cur.fetchall()]
    s_at, e_at = a["start_at"].astimezone(TZ), a["end_at"].astimezone(TZ)
    return {
        "booking_id": a["appointment_id"], "appointment_id": a["appointment_id"],
        "status": a["status"],
        "barber": {"id": a["barber_id"], "name": a["barber_name"]},
        "customer": {"first_name": a["customer_first_name"], "last_name": a["customer_last_name"],
                     "phone": a["customer_phone"]},
        "date": s_at.date().isoformat(), "start_time": hhmm(s_at), "end_time": hhmm(e_at),
        "start_at": s_at.isoformat(), "end_at": e_at.isoformat(),
        "services": services,
        "total_price": money(a["total_price"]),
        "total_duration_minutes": a["total_duration_minutes"],
    }


# ---------- Public pages / catalog ----------
@app.get("/")
def index():
    return render_template("booking.html")


@app.get("/healthz")
def healthz():
    """Liveness + database check for the hosting platform."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return jsonify(ok=True)


@app.get("/api/config")
def config():
    today = now_local().date()
    days = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        days.append({"date": d.isoformat(), "dow": DOW[d.weekday()], "day": d.day,
                     "month": MONTHS[d.month - 1], "label": f"{DOW[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"})
    return jsonify(timezone=str(TZ), today=today.isoformat(),
                   slot_step_minutes=SLOT_STEP_MINUTES, dates=days)


@app.get("/api/services")
def list_services():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT service_id, service_name, description, price, duration_minutes "
                    "FROM services WHERE is_active ORDER BY service_id")
        rows = cur.fetchall()
    return jsonify(services=[{"id": r["service_id"], "name": r["service_name"],
                              "description": r["description"], "price": money(r["price"]),
                              "duration_minutes": r["duration_minutes"]} for r in rows])


@app.get("/api/barbers")
def list_barbers():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT barber_id, first_name, last_name, specialization "
                    "FROM barbers WHERE is_active ORDER BY barber_id")
        rows = cur.fetchall()
    return jsonify(barbers=[{"id": r["barber_id"], "name": r["first_name"],
                             "full_name": f"{r['first_name']} {r['last_name']}",
                             "initials": (r["first_name"][:1] + r["last_name"][:1]).upper(),
                             "specialization": r["specialization"]} for r in rows])


@app.get("/api/barbers/<int:barber_id>/working-hours")
def barber_working_hours(barber_id):
    with db() as conn, conn.cursor() as cur:
        get_active_barber(cur, barber_id)
        cur.execute("SELECT day_of_week, start_time, end_time FROM barber_working_hours "
                    "WHERE barber_id = %s ORDER BY day_of_week, start_time", (barber_id,))
        hours = [{"day_of_week": r["day_of_week"], "start_time": hhmm(r["start_time"]),
                  "end_time": hhmm(r["end_time"])} for r in cur.fetchall()]
        cur.execute("SELECT id, day_of_week, start_time, end_time, label FROM barber_breaks "
                    "WHERE barber_id = %s ORDER BY day_of_week NULLS FIRST, start_time", (barber_id,))
        breaks = [{"id": r["id"], "day_of_week": r["day_of_week"], "start_time": hhmm(r["start_time"]),
                   "end_time": hhmm(r["end_time"]), "label": r["label"]} for r in cur.fetchall()]
    return jsonify(barber_id=barber_id, timezone=str(TZ), working_hours=hours, breaks=breaks)


@app.get("/api/availability")
def availability():
    barber_id = parse_int(request.args.get("barber_id"), "barber_id")
    day = parse_day(request.args.get("date"))
    raw_ids = [x for x in request.args.get("service_ids", "").split(",") if x.strip()]
    ids = parse_service_ids([x.strip() for x in raw_ids])
    now = now_local()
    if not (now.date() <= day <= now.date() + timedelta(days=DAYS_AHEAD - 1)):
        raise ApiError(400, "date_unavailable", "This date is not available for booking")
    with db() as conn, conn.cursor() as cur:
        get_active_barber(cur, barber_id)
        rows = load_services(cur, ids)
        price, duration = totals(rows)
        ctx = load_day_context(cur, barber_id, day)
    slots = compute_slots(ctx, duration, now)
    return jsonify(barber_id=barber_id, date=day.isoformat(), total_price=money(price),
                   total_duration_minutes=duration, works_that_day=bool(ctx.windows), slots=slots)


@app.get("/api/busy")
def busy():
    """Deprecated compatibility endpoint: unavailable hourly slots for 60 minutes."""
    barber_id = parse_int(request.args.get("barber_id"), "barber_id")
    day = parse_day(request.args.get("date"))
    with db() as conn, conn.cursor() as cur:
        get_active_barber(cur, barber_id)
        ctx = load_day_context(cur, barber_id, day)
    slots = compute_slots(ctx, 60, now_local())
    return jsonify(busy=[s["time"] for s in slots if not s["available"] and s["time"].endswith(":00")])


# ---------- Booking ----------
def create_booking(data):
    first = str(data.get("first_name", "")).strip()
    last = str(data.get("last_name", "")).strip()
    phone = normalize_phone(data.get("phone"))
    if not first or not last:
        raise ApiError(400, "invalid_name", "Введите имя и фамилию")
    if len(first) > 50 or len(last) > 50:
        raise ApiError(400, "invalid_name", "Имя и фамилия — не длиннее 50 символов")
    if not re.fullmatch(r"\+\d{10,15}", phone) or len(phone) > 20:
        raise ApiError(400, "invalid_phone", "Введите телефон в формате +77021234567")

    barber_id = parse_int(data.get("barber_id"), "barber_id")
    service_ids = parse_service_ids(data.get("service_ids"))
    day = parse_day(data.get("date"))
    start_t = parse_hhmm(data.get("time"))
    key = data.get("idempotency_key")
    if key is not None and not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key)):
        raise ApiError(400, "invalid_idempotency_key", "Invalid idempotency key")

    start = local_dt(day, start_t)
    # NOTE: client-supplied price/duration/total fields are never read.

    with db() as conn, conn.cursor() as cur:
        if key:
            cur.execute("SELECT appointment_id FROM appointments WHERE idempotency_key = %s", (key,))
            existing = cur.fetchone()
            if existing:
                return fetch_booking(cur, existing["appointment_id"]), False

        # Shop-wide mode: serialize ALL booking attempts (any barber) until commit,
        # otherwise two different barbers could be booked for the same time at once.
        if SHOP_WIDE_BLOCKING:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SHOP_LOCK_KEY,))

        # Row lock: all booking attempts for this barber are serialized from here
        # until commit.
        get_active_barber(cur, barber_id, lock=True)
        rows = load_services(cur, service_ids)
        total_price, total_minutes = totals(rows)
        end = start + timedelta(minutes=total_minutes)

        now = now_local()
        if not (now.date() <= day <= now.date() + timedelta(days=DAYS_AHEAD - 1)):
            raise ApiError(400, "date_unavailable", "This date is not available for booking")
        if start <= now:
            raise ApiError(400, "time_in_past", "This time has already passed")

        ctx = load_day_context(cur, barber_id, day)
        problem = interval_problem(ctx, start, end)
        if problem == "slot_conflict":
            raise ApiError(409, "slot_conflict", "This time is taken or overlaps another booking.")
        if problem:
            raise ApiError(422, problem, "The appointment does not fit the barber's working hours or breaks.")
        aligned = any(ws <= start < we and ((start - ws).total_seconds() // 60) % SLOT_STEP_MINUTES == 0
                      for ws, we in ctx.windows)
        if not aligned:
            raise ApiError(400, "time_not_offered", "This start time is not offered")

        cur.execute("SELECT client_id FROM clients WHERE phone_number = %s "
                    "AND LOWER(first_name) = LOWER(%s) AND LOWER(last_name) = LOWER(%s) LIMIT 1",
                    (phone, first, last))
        row = cur.fetchone()
        if row:
            client_id = row["client_id"]
        else:
            cur.execute("INSERT INTO clients (first_name, last_name, phone_number, email, password_hash) "
                        "VALUES (%s, %s, %s, %s, crypt(%s, gen_salt('bf', 12))) RETURNING client_id",
                        (first, last, phone, f"guest_{uuid.uuid4().hex[:12]}@guest.local", uuid.uuid4().hex))
            client_id = cur.fetchone()["client_id"]

        cur.execute(
            """INSERT INTO appointments
               (client_id, barber_id, appointment_date, start_time, status, start_at, end_at,
                total_price, total_duration_minutes, customer_first_name, customer_last_name,
                customer_phone, idempotency_key)
               VALUES (%s,%s,%s,%s,'confirmed',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING appointment_id""",
            (client_id, barber_id, day, start_t, start, end, total_price, total_minutes,
             first, last, phone, key))
        appointment_id = cur.fetchone()["appointment_id"]
        cur.executemany(
            "INSERT INTO appointment_services (appointment_id, service_id, service_name_snapshot, "
            "price_snapshot, duration_minutes_snapshot) VALUES (%s,%s,%s,%s,%s)",
            [(appointment_id, r["service_id"], r["service_name"], r["price"], r["duration_minutes"])
             for r in rows])
        return fetch_booking(cur, appointment_id), True


@app.post("/api/book")
def book():
    data = json_body()
    try:
        booking, created = create_booking(data)
    except errors.ExclusionViolation:
        raise ApiError(409, "slot_conflict", "This time was just taken. Please choose another.")
    except errors.UniqueViolation as e:
        name = e.diag.constraint_name
        if name == LEGACY_SLOT_INDEX:
            raise ApiError(409, "slot_conflict", "This time was just taken. Please choose another.")
        if name == IDEMPOTENCY_INDEX:  # double submit racing with itself
            with db() as conn, conn.cursor() as cur:
                cur.execute("SELECT appointment_id FROM appointments WHERE idempotency_key = %s",
                            (data.get("idempotency_key"),))
                row = cur.fetchone()
                booking, created = fetch_booking(cur, row["appointment_id"]), False
        else:
            raise
    return jsonify(ok=True, created=created, booking=booking, appointment_id=booking["booking_id"]), \
        (201 if created else 200)


@app.get("/api/bookings/<int:booking_id>")
def get_booking(booking_id):
    """Booking details; caller must prove ownership with the phone number (or be admin)."""
    is_admin = bool(ADMIN_TOKEN) and hmac.compare_digest(
        request.headers.get("X-Admin-Token", "").encode(), ADMIN_TOKEN.encode())
    with db() as conn, conn.cursor() as cur:
        booking = fetch_booking(cur, booking_id)
    if booking is None or not (is_admin or booking["customer"]["phone"] == normalize_phone(request.args.get("phone"))):
        raise ApiError(404, "booking_not_found", "Booking not found")
    return jsonify(ok=True, booking=booking)


@app.post("/api/cancel")
def cancel():
    data = json_body()
    appointment_id = parse_int(data.get("appointment_id"), "appointment_id")
    phone = normalize_phone(data.get("phone"))
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE appointments SET status = 'cancelled' "
                    "WHERE appointment_id = %s AND customer_phone = %s "
                    "AND status NOT IN ('cancelled', 'completed') RETURNING appointment_id",
                    (appointment_id, phone))
        if cur.fetchone() is None:
            raise ApiError(404, "booking_not_found",
                           "Запись не найдена, уже отменена или телефон не совпадает")
    return jsonify(ok=True, message=f"Запись №{appointment_id} отменена.")


# ---------- Admin API (X-Admin-Token header; disabled unless ADMIN_TOKEN is set) ----------
@app.get("/api/admin/schedule")
@require_admin
def admin_schedule():
    day = parse_day(request.args.get("date", now_local().date().isoformat()))
    barber = request.args.get("barber_id")
    lo = local_dt(day, time(0, 0))
    params = [lo, lo + timedelta(days=1)]
    extra = ""
    if barber:
        extra = " AND a.barber_id = %s"
        params.append(parse_int(barber, "barber_id"))
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT a.appointment_id, a.status, a.start_at, a.end_at, a.total_price,
                       a.total_duration_minutes, b.barber_id, b.first_name AS barber,
                       a.customer_first_name || ' ' || a.customer_last_name AS client,
                       a.customer_phone,
                       COALESCE(json_agg(json_build_object(
                           'service_id', s.service_id, 'name', s.service_name_snapshot,
                           'price', s.price_snapshot::text,
                           'duration_minutes', s.duration_minutes_snapshot) ORDER BY s.service_id)
                           FILTER (WHERE s.service_id IS NOT NULL), '[]'::json) AS services
                FROM appointments a
                JOIN barbers b ON b.barber_id = a.barber_id
                LEFT JOIN appointment_services s ON s.appointment_id = a.appointment_id
                WHERE a.start_at >= %s AND a.start_at < %s{extra}
                GROUP BY a.appointment_id, b.barber_id
                ORDER BY a.start_at, a.appointment_id""", params)
        rows = cur.fetchall()
    out = [{"appointment_id": r["appointment_id"], "status": r["status"], "barber_id": r["barber_id"],
            "barber": r["barber"], "client": r["client"], "phone_number": r["customer_phone"],
            "time": hhmm(r["start_at"].astimezone(TZ)), "end_time": hhmm(r["end_at"].astimezone(TZ)),
            "total_price": money(r["total_price"]), "total_duration_minutes": r["total_duration_minutes"],
            "services": r["services"]} for r in rows]
    return jsonify(date=day.isoformat(), appointments=out)


def _service_fields(data, partial):
    out = {}
    if "name" in data or not partial:
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 100:
            raise ApiError(400, "invalid_name", "name required (max 100 chars)")
        out["service_name"] = name
    if "description" in data:
        out["description"] = None if data["description"] is None else str(data["description"])
    if "price" in data or not partial:
        try:
            p = Decimal(str(data.get("price")))
        except InvalidOperation:
            raise ApiError(400, "invalid_price", "Invalid price")
        if not p.is_finite() or p < 0 or p != p.quantize(Decimal("0.01")) or p >= Decimal("100000000"):
            raise ApiError(400, "invalid_price", "price must be >= 0 with at most 2 decimals")
        out["price"] = p
    if "duration_minutes" in data or not partial:
        d = parse_int(data.get("duration_minutes"), "duration")
        if not 1 <= d <= 720:
            raise ApiError(400, "invalid_duration", "duration_minutes must be 1..720")
        out["duration_minutes"] = d
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ApiError(400, "invalid_is_active", "is_active must be boolean")
        out["is_active"] = data["is_active"]
    return out


@app.get("/api/admin/services")
@require_admin
def admin_list_services():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT service_id AS id, service_name AS name, description, price::text AS price, "
                    "duration_minutes, is_active FROM services ORDER BY service_id")
        return jsonify(services=cur.fetchall())


@app.post("/api/admin/services")
@require_admin
def admin_create_service():
    f = _service_fields(json_body(), partial=False)
    f.setdefault("description", None)
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO services (service_name, description, price, duration_minutes) "
                        "VALUES (%(service_name)s, %(description)s, %(price)s, %(duration_minutes)s) "
                        "RETURNING service_id", f)
            return jsonify(ok=True, id=cur.fetchone()["service_id"]), 201
    except errors.UniqueViolation:
        raise ApiError(409, "duplicate_service", "A service with this name already exists")


@app.patch("/api/admin/services/<int:service_id>")
@require_admin
def admin_update_service(service_id):
    f = _service_fields(json_body(), partial=True)
    if not f:
        raise ApiError(400, "nothing_to_update", "No updatable fields supplied")
    sets = ", ".join(f"{k} = %({k})s" for k in f)  # keys come from a fixed whitelist above
    f["id"] = service_id
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE services SET {sets} WHERE service_id = %(id)s RETURNING service_id", f)
            if cur.fetchone() is None:
                raise ApiError(404, "service_not_found", "Service not found")
    except errors.UniqueViolation:
        raise ApiError(409, "duplicate_service", "A service with this name already exists")
    return jsonify(ok=True)


@app.patch("/api/admin/barbers/<int:barber_id>")
@require_admin
def admin_update_barber(barber_id):
    data = json_body()
    sets, params = [], {"id": barber_id}
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ApiError(400, "invalid_is_active", "is_active must be boolean")
        sets.append("is_active = %(is_active)s"); params["is_active"] = data["is_active"]
    if "specialization" in data:
        sets.append("specialization = %(spec)s"); params["spec"] = str(data["specialization"])[:100]
    if not sets:
        raise ApiError(400, "nothing_to_update", "No updatable fields supplied")
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE barbers SET {', '.join(sets)} WHERE barber_id = %(id)s RETURNING barber_id", params)
        if cur.fetchone() is None:
            raise ApiError(404, "barber_not_found", "Barber not found")
    return jsonify(ok=True)


@app.put("/api/admin/barbers/<int:barber_id>/working-hours")
@require_admin
def admin_set_hours(barber_id):
    items = json_body().get("hours")
    if not isinstance(items, list):
        raise ApiError(400, "invalid_hours", "hours must be a list")
    parsed = []
    for it in items:
        dow = parse_int(it.get("day_of_week") if isinstance(it, dict) else None, "day_of_week")
        s, e = parse_hhmm(it.get("start_time")), parse_hhmm(it.get("end_time"))
        if not 0 <= dow <= 6 or e <= s:
            raise ApiError(400, "invalid_hours", "day_of_week 0..6 and end_time > start_time required")
        parsed.append((dow, s, e))
    parsed.sort()
    for a, b in zip(parsed, parsed[1:]):
        if a[0] == b[0] and b[1] < a[2]:
            raise ApiError(400, "overlapping_hours", "Working intervals overlap on the same day")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM barbers WHERE barber_id = %s FOR UPDATE", (barber_id,))
        if cur.fetchone() is None:
            raise ApiError(404, "barber_not_found", "Barber not found")
        cur.execute("DELETE FROM barber_working_hours WHERE barber_id = %s", (barber_id,))
        cur.executemany("INSERT INTO barber_working_hours (barber_id, day_of_week, start_time, end_time) "
                        "VALUES (%s,%s,%s,%s)", [(barber_id, d, s, e) for d, s, e in parsed])
    return jsonify(ok=True, count=len(parsed))


@app.post("/api/admin/barbers/<int:barber_id>/breaks")
@require_admin
def admin_add_break(barber_id):
    data = json_body()
    dow = data.get("day_of_week")
    dow = None if dow is None else parse_int(dow, "day_of_week")
    s, e = parse_hhmm(data.get("start_time")), parse_hhmm(data.get("end_time"))
    if (dow is not None and not 0 <= dow <= 6) or e <= s:
        raise ApiError(400, "invalid_break", "Invalid break")
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO barber_breaks (barber_id, day_of_week, start_time, end_time, label) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (barber_id, dow, s, e, str(data.get("label") or "")[:100] or None))
            return jsonify(ok=True, id=cur.fetchone()["id"]), 201
    except errors.ForeignKeyViolation:
        raise ApiError(404, "barber_not_found", "Barber not found")


@app.delete("/api/admin/breaks/<int:break_id>")
@require_admin
def admin_delete_break(break_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM barber_breaks WHERE id = %s RETURNING id", (break_id,))
        if cur.fetchone() is None:
            raise ApiError(404, "not_found", "Break not found")
    return jsonify(ok=True)


def _parse_dt(raw):
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        raise ApiError(400, "invalid_datetime", "Invalid ISO datetime")
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


@app.post("/api/admin/barbers/<int:barber_id>/time-off")
@require_admin
def admin_add_time_off(barber_id):
    data = json_body()
    s, e = _parse_dt(data.get("start_at")), _parse_dt(data.get("end_at"))
    if e <= s:
        raise ApiError(400, "invalid_range", "end_at must be after start_at")
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO barber_time_off (barber_id, start_at, end_at, reason) "
                        "VALUES (%s,%s,%s,%s) RETURNING id",
                        (barber_id, s, e, str(data.get("reason") or "")[:200] or None))
            return jsonify(ok=True, id=cur.fetchone()["id"]), 201
    except errors.ForeignKeyViolation:
        raise ApiError(404, "barber_not_found", "Barber not found")


@app.delete("/api/admin/time-off/<int:off_id>")
@require_admin
def admin_delete_time_off(off_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM barber_time_off WHERE id = %s RETURNING id", (off_id,))
        if cur.fetchone() is None:
            raise ApiError(404, "not_found", "Time-off not found")
    return jsonify(ok=True)


# ---------- Startup: make sure the database has the upgraded structure ----------
def ensure_schema():
    """Apply schema.sql (idempotent) if the DB is missing the upgraded structure.
    An advisory lock makes concurrent gunicorn workers take turns."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SHOP_LOCK_KEY + 1,))
            cur.execute(
                "SELECT (EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='services' AND column_name='description') "
                "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conname='appointments_no_barber_overlap')) AS ok")
            if cur.fetchone()["ok"]:
                return
            log.warning("Database schema is outdated - applying %s", SCHEMA_FILE)
            with open(SCHEMA_FILE, encoding="utf-8") as f:
                cur.execute(f.read())
            log.info("schema.sql applied successfully")
    except Exception:
        log.exception("ensure_schema failed - run schema.sql manually with psql")


ensure_schema()


if __name__ == "__main__":
    app.run(debug=DEBUG)