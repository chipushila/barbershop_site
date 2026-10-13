CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;


CREATE TABLE IF NOT EXISTS clients (
    client_id SERIAL PRIMARY KEY,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS barbers (
    barber_id SERIAL PRIMARY KEY,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    phone_number VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    service_id SERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    duration_minutes INT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id SERIAL PRIMARY KEY,
    client_id INT NOT NULL,
    barber_id INT NOT NULL,
    appointment_date DATE NOT NULL,
    start_time TIME NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
    CONSTRAINT fk_appointments_client FOREIGN KEY (client_id) REFERENCES clients(client_id) ON DELETE CASCADE,
    CONSTRAINT fk_appointments_barber FOREIGN KEY (barber_id) REFERENCES barbers(barber_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS appointment_services (
    appointment_id INT NOT NULL,
    service_id INT NOT NULL,
    PRIMARY KEY (appointment_id, service_id),
    CONSTRAINT fk_app_services_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id) ON DELETE CASCADE,
    CONSTRAINT fk_app_services_service FOREIGN KEY (service_id) REFERENCES services(service_id) ON DELETE CASCADE
);

-- ---------- Upgrade: helper trigger function ----------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


ALTER TABLE services
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'services_price_nonneg') THEN
        ALTER TABLE services ADD CONSTRAINT services_price_nonneg CHECK (price >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'services_duration_positive') THEN
        ALTER TABLE services ADD CONSTRAINT services_duration_positive CHECK (duration_minutes > 0);
    END IF;
END $$;

-- Fails if existing data has duplicate service names: merge them first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_services_name ON services (lower(service_name));

DROP TRIGGER IF EXISTS trg_services_updated_at ON services;
CREATE TRIGGER trg_services_updated_at BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Upgrade: barbers ----------
ALTER TABLE barbers
    ADD COLUMN IF NOT EXISTS specialization VARCHAR(100),
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- ---------- Upgrade: per-barber schedule ----------
-- day_of_week follows Python's date.weekday(): 0 = Monday ... 6 = Sunday.
CREATE TABLE IF NOT EXISTS barber_working_hours (
    id          SERIAL PRIMARY KEY,
    barber_id   INT      NOT NULL REFERENCES barbers(barber_id) ON DELETE CASCADE,
    day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time  TIME     NOT NULL,
    end_time    TIME     NOT NULL,
    CHECK (end_time > start_time),
    UNIQUE (barber_id, day_of_week, start_time)
);

-- Recurring breaks. day_of_week NULL = every day.
CREATE TABLE IF NOT EXISTS barber_breaks (
    id          SERIAL PRIMARY KEY,
    barber_id   INT  NOT NULL REFERENCES barbers(barber_id) ON DELETE CASCADE,
    day_of_week SMALLINT CHECK (day_of_week BETWEEN 0 AND 6),
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    label       VARCHAR(100),
    CHECK (end_time > start_time)
);

-- One-off unavailability: days off, vacations.
CREATE TABLE IF NOT EXISTS barber_time_off (
    id        SERIAL PRIMARY KEY,
    barber_id INT         NOT NULL REFERENCES barbers(barber_id) ON DELETE CASCADE,
    start_at  TIMESTAMPTZ NOT NULL,
    end_at    TIMESTAMPTZ NOT NULL,
    reason    VARCHAR(200),
    CHECK (end_at > start_at)
);
CREATE INDEX IF NOT EXISTS ix_time_off_barber ON barber_time_off (barber_id, start_at, end_at);

-- ---------- Upgrade: appointment_services snapshots ----------
ALTER TABLE appointment_services
    ADD COLUMN IF NOT EXISTS service_name_snapshot     VARCHAR(100),
    ADD COLUMN IF NOT EXISTS price_snapshot            DECIMAL(10, 2),
    ADD COLUMN IF NOT EXISTS duration_minutes_snapshot INT;

-- Deleting a service must not silently delete booking history: deactivate instead.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'fk_app_services_service' AND confdeltype = 'c') THEN
        ALTER TABLE appointment_services DROP CONSTRAINT fk_app_services_service;
        ALTER TABLE appointment_services ADD CONSTRAINT fk_app_services_service
            FOREIGN KEY (service_id) REFERENCES services(service_id) ON DELETE RESTRICT;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_app_services_service ON appointment_services (service_id);

-- ---------- Upgrade: appointments ----------
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS start_at               TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS end_at                 TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_price            DECIMAL(10, 2),
    ADD COLUMN IF NOT EXISTS total_duration_minutes INT,
    ADD COLUMN IF NOT EXISTS customer_first_name    VARCHAR(50),
    ADD COLUMN IF NOT EXISTS customer_last_name     VARCHAR(50),
    ADD COLUMN IF NOT EXISTS customer_phone         VARCHAR(20),
    ADD COLUMN IF NOT EXISTS idempotency_key        VARCHAR(64),
    ADD COLUMN IF NOT EXISTS created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at             TIMESTAMPTZ NOT NULL DEFAULT now();

-- =====================================================================
-- SEED DATA (only inserts what is missing; never overwrites admin edits)
-- =====================================================================

-- The 10 services: matched by name (case-insensitive). Existing price/duration/
-- name/is_active are NEVER touched; only a missing description is filled in.
INSERT INTO services (service_name, description, price, duration_minutes) VALUES
 ('Classic Haircut',             'Precision cut, finished and styled.',                 8000.00,  45),
 ('Beard Trim & Styling',        'Shape, trim and style your beard.',                   4000.00,  30),
 ('Hair Coloring',               'Full colour service with professional products.',    15000.00,  90),
 ('Royal Head Shave',            'Hot-towel straight-razor head shave.',                6000.00,  40),
 ('Hair Washing & Styling',      'Wash, conditioning and styling.',                     3000.00,  20),
 ('Father & Son Haircut Combo',  'Two haircuts, one visit.',                           13000.00,  60),
 ('Grey Hair Camouflage',        'Natural-looking grey coverage.',                      7000.00,  30),
 ('Face Waxing & Eyebrow Care',  'Gentle waxing and eyebrow shaping.',                  3500.00,  25),
 ('Deep Scalp Treatment',        'Cleansing and nourishing scalp therapy.',             9000.00,  45),
 ('VIP Full Grooming Package',   'Haircut, beard, shave, wash and care in one visit.', 22000.00, 120)
ON CONFLICT (lower(service_name)) DO UPDATE
    SET description = EXCLUDED.description
    WHERE services.description IS NULL;

-- Barbers / clients: only into EMPTY tables (so re-running never duplicates).
INSERT INTO barbers (first_name, last_name, phone_number)
SELECT v.a, v.b, v.c FROM (VALUES
('Ayan', 'Murat', '+77011112233'),
('Dias', 'Nariman', '+77022223344'),
('Arman', 'Serik', '+77033334455'),
('Daniyar', 'Kuat', '+77044445566'),
('Erzhan', 'Bolat', '+77055556677'),
('Nurzhan', 'Sultan', '+77066667788'),
('Timur', 'Aliev', '+77077778899'),
('Bauyrzhan', 'Kassym', '+77088889900'),
('Kairat', 'Zhan', '+77099990011'),
('Sanzhar', 'Bek', '+77000001122')
) AS v(a, b, c)
WHERE NOT EXISTS (SELECT 1 FROM barbers);

UPDATE barbers SET specialization = 'Senior barber' WHERE first_name = 'Ayan' AND specialization IS NULL;
UPDATE barbers SET specialization = 'Style expert'  WHERE first_name = 'Dias' AND specialization IS NULL;

INSERT INTO clients (first_name, last_name, phone_number, email, password_hash)
SELECT v.a, v.b, v.c, v.d, v.e FROM (VALUES
('Yerkhan', 'Khamit', '+77001234567', 'yerkhan@example.com','hash_pass_101'),
('Yersultan', 'Amanshol', '+77059998877', 'yersultan@example.com', 'hash_pass_102'),
('Ali', 'Nurtas', '+77011223344', 'ali@example.com', 'hash_pass_103'),
('Ruslan', 'Ibrayev', '+77015554433', 'ruslan@example.com', 'hash_pass_104'),
('Dauren', 'Saparov', '+77026665544', 'dauren@example.com', 'hash_pass_105'),
('Aidos', 'Tulegenov', '+77037776655', 'aidos@example.com', 'hash_pass_106'),
('Madi', 'Kenzheyev', '+77048887766', 'madi@example.com', 'hash_pass_107'),
('Askhat', 'Ospanov', '+77059998877', 'askhat@example.com', 'hash_pass_108'),
('Bektur', 'Zhuma', '+77061112233', 'bektur@example.com', 'hash_pass_109'),
('Almas', 'Nusip', '+77072223344', 'almas@example.com', 'hash_pass_110'),
('Kuanysh', 'Mami', '+77083334455', 'kuanysh@example.com', 'hash_pass_111'),
('Olzhas', 'Rakhim', '+77094445566', 'olzhas@example.com', 'hash_pass_112'),
('Sultan', 'Baizhan', '+77005556677', 'sultan@example.com', 'hash_pass_113'),
('Ilyas', 'Kalykov', '+77016667788', 'ilyas@example.com', 'hash_pass_114'),
('Kairat', 'Nurtas', '+77027778899', 'kairat@example.com', 'hash_pass_115'),
('Zhandos', 'Tazhi', '+77038889900', 'zhandos@example.com', 'hash_pass_116'),
('Adil', 'Zhanibek', '+77049990011', 'adil@example.com', 'hash_pass_117'),
('Darkhan', 'Bazar', '+77050001122', 'darkhan@example.com', 'hash_pass_118'),
('Azamat', 'Suleimen', '+77061112244', 'azamat@example.com', 'hash_pass_119'),
('Yerlan', 'Muran', '+77072223355', 'yerlan@example.com', 'hash_pass_120')
) AS v(a, b, c, d, e)
WHERE NOT EXISTS (SELECT 1 FROM clients);

-- Default working hours 10:00-20:00 daily, only for barbers that have none.
-- Edit per barber afterwards (admin API) to give barbers different schedules.
INSERT INTO barber_working_hours (barber_id, day_of_week, start_time, end_time)
SELECT b.barber_id, d, TIME '10:00', TIME '20:00'
FROM barbers b CROSS JOIN generate_series(0, 6) AS d
WHERE NOT EXISTS (SELECT 1 FROM barber_working_hours w WHERE w.barber_id = b.barber_id);

-- (Random demo appointments removed for production: real bookings come from the app
-- or from import_appointments.py.)

-- =====================================================================
-- BACKFILL existing / seeded rows, then lock the new columns down
-- =====================================================================
UPDATE appointment_services s
SET service_name_snapshot     = sv.service_name,
    price_snapshot            = sv.price,
    duration_minutes_snapshot = sv.duration_minutes
FROM services sv
WHERE sv.service_id = s.service_id AND s.service_name_snapshot IS NULL;

ALTER TABLE appointment_services
    ALTER COLUMN service_name_snapshot     SET NOT NULL,
    ALTER COLUMN price_snapshot            SET NOT NULL,
    ALTER COLUMN duration_minutes_snapshot SET NOT NULL;

-- Appointments without any service get 60 minutes (the old /api/busy default).
UPDATE appointments a
SET total_price            = COALESCE(t.p, 0),
    total_duration_minutes = CASE WHEN COALESCE(t.d, 0) = 0 THEN 60 ELSE t.d END
FROM (
    SELECT ap.appointment_id,
           SUM(s.price_snapshot)            AS p,
           SUM(s.duration_minutes_snapshot) AS d
    FROM appointments ap
    LEFT JOIN appointment_services s ON s.appointment_id = ap.appointment_id
    GROUP BY ap.appointment_id
) t
WHERE a.appointment_id = t.appointment_id
  AND (a.total_price IS NULL OR a.total_duration_minutes IS NULL);

UPDATE appointments
SET start_at = (appointment_date + start_time) AT TIME ZONE 'Asia/Almaty'
WHERE start_at IS NULL;

UPDATE appointments
SET end_at = start_at + total_duration_minutes * INTERVAL '1 minute'
WHERE end_at IS NULL;

UPDATE appointments a
SET customer_first_name = c.first_name,
    customer_last_name  = c.last_name,
    customer_phone      = c.phone_number
FROM clients c
WHERE c.client_id = a.client_id AND a.customer_first_name IS NULL;

ALTER TABLE appointments
    ALTER COLUMN start_at               SET NOT NULL,
    ALTER COLUMN end_at                 SET NOT NULL,
    ALTER COLUMN total_price            SET NOT NULL,
    ALTER COLUMN total_duration_minutes SET NOT NULL,
    ALTER COLUMN customer_first_name    SET NOT NULL,
    ALTER COLUMN customer_last_name     SET NOT NULL,
    ALTER COLUMN customer_phone         SET NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_interval_valid') THEN
        ALTER TABLE appointments ADD CONSTRAINT appointments_interval_valid
            CHECK (end_at > start_at AND total_duration_minutes > 0 AND total_price >= 0);
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_appointments_updated_at ON appointments;
CREATE TRIGGER trg_appointments_updated_at BEFORE UPDATE ON appointments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_idempotency
    ON appointments (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_appointments_barber_start ON appointments (barber_id, start_at);

-- Pre-flight: readable failure if existing data already overlaps for one barber.
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n
    FROM appointments a
    JOIN appointments b ON a.barber_id = b.barber_id
     AND a.appointment_id < b.appointment_id
     AND a.status <> 'cancelled' AND b.status <> 'cancelled'
     AND a.start_at < b.end_at AND a.end_at > b.start_at;
    IF n > 0 THEN
        RAISE EXCEPTION 'MANUAL FIX NEEDED: % overlapping non-cancelled appointment pair(s) for the same barber. Cancel or move them, then re-run this file.', n;
    END IF;
END $$;

-- The concurrency guarantee: the database itself refuses two non-cancelled
-- bookings of the SAME barber with overlapping [start_at, end_at) intervals.
-- Different barbers never conflict; back-to-back bookings are allowed.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_no_barber_overlap') THEN
        ALTER TABLE appointments ADD CONSTRAINT appointments_no_barber_overlap
            EXCLUDE USING gist (
                barber_id WITH =,
                tstzrange(start_at, end_at, '[)') WITH &&
            ) WHERE (status <> 'cancelled');
    END IF;
END $$;

-- Original: hash any plaintext seed passwords (idempotent).
UPDATE clients
SET password_hash = crypt(password_hash, gen_salt('bf', 12))
WHERE password_hash NOT LIKE '$2%';