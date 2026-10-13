"""Import appointments from the exported CSV into PostgreSQL (idempotent, no deletes).

Usage:  DATABASE_URL=postgresql://... python import_appointments.py data-1791900799410.csv

* Existing appointment_ids are skipped (ON CONFLICT DO NOTHING), so re-running is safe.
* The CSV has no clients table: a missing client_id gets a placeholder client built from
  the customer_* columns on the row.
* The CSV has no appointment_services table, so imported rows keep their totals but have
  no per-service lines. Re-export appointment_services (and clients) to restore them.
"""
import csv
import os
import sys
import uuid

import psycopg2


def main(path):
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    imported = skipped = 0
    try:
        with conn, conn.cursor() as cur, open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cur.execute("SELECT 1 FROM appointments WHERE appointment_id = %s", (r["appointment_id"],))
                if cur.fetchone():
                    skipped += 1
                    continue
                cur.execute("SELECT 1 FROM clients WHERE client_id = %s", (r["client_id"],))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO clients (client_id, first_name, last_name, phone_number, email, password_hash) "
                        "VALUES (%s,%s,%s,%s,%s, crypt(%s, gen_salt('bf', 12)))",
                        (r["client_id"], r["customer_first_name"], r["customer_last_name"],
                         r["customer_phone"], f"imported_{uuid.uuid4().hex[:12]}@guest.local", uuid.uuid4().hex))
                cur.execute(
                    "INSERT INTO appointments (appointment_id, client_id, barber_id, appointment_date, start_time, "
                    "status, start_at, end_at, total_price, total_duration_minutes, customer_first_name, "
                    "customer_last_name, customer_phone, idempotency_key, created_at, updated_at) "
                    "VALUES (%(appointment_id)s,%(client_id)s,%(barber_id)s,%(appointment_date)s,%(start_time)s,"
                    "%(status)s,%(start_at)s,%(end_at)s,%(total_price)s,%(total_duration_minutes)s,"
                    "%(customer_first_name)s,%(customer_last_name)s,%(customer_phone)s,"
                    "NULLIF(%(idempotency_key)s,''),%(created_at)s,%(updated_at)s)", r)
                imported += 1
            # keep SERIAL sequences ahead of the imported ids
            cur.execute("SELECT setval(pg_get_serial_sequence('appointments','appointment_id'), "
                        "(SELECT COALESCE(MAX(appointment_id),1) FROM appointments))")
            cur.execute("SELECT setval(pg_get_serial_sequence('clients','client_id'), "
                        "(SELECT COALESCE(MAX(client_id),1) FROM clients))")
    finally:
        conn.close()
    print(f"imported={imported} skipped={skipped}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data-1791900799410.csv")
