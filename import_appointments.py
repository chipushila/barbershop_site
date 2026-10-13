import csv
import os
import sys
import uuid
import psycopg2

DB_URL = os.getenv("DATABASE_URL")

def main(csv_path):
    if not DB_URL:
        print("Ошибка: переменная окружения DATABASE_URL не задана!")
        sys.exit(1)

    conn = psycopg2.connect(DB_URL)
    try:
        with conn, conn.cursor() as cur:
            with open(csv_path, newline="", encoding="utf-8") as f:
                imported = 0
                skipped = 0
                for r in csv.DictReader(f):
                    app_id = r["appointment_id"]

                    # Проверяем, есть ли уже такая запись
                    cur.execute("SELECT 1 FROM appointments WHERE appointment_id = %s", (app_id,))
                    if cur.fetchone():
                        skipped += 1
                        continue

                    # Проверяем и добавляем клиента при необходимости
                    cur.execute("SELECT 1 FROM clients WHERE client_id = %s", (r["client_id"],))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO clients (client_id, first_name, last_name, phone_number, email, password_hash) "
                            "VALUES (%s, %s, %s, %s, %s, crypt(%s, gen_salt('bf', 12)))",
                            (
                                r["client_id"],
                                r["customer_first_name"],
                                r["customer_last_name"],
                                r["customer_phone"],
                                f"imported_{uuid.uuid4().hex[:12]}@guest.local",
                                uuid.uuid4().hex,
                            ),
                        )

                    # Гарантированно задаем уникальный UUID для каждой записи
                    key = r.get("idempotency_key")
                    if not key or str(key).strip().lower() in ("", "none", "null"):
                        r["idempotency_key"] = str(uuid.uuid4())

                    cur.execute(
                        """
                        INSERT INTO appointments (
                            appointment_id, client_id, barber_id, appointment_date, start_time,
                            status, start_at, end_at, total_price, total_duration_minutes,
                            customer_first_name, customer_last_name, customer_phone,
                            idempotency_key, created_at, updated_at
                        ) VALUES (
                            %(appointment_id)s, %(client_id)s, %(barber_id)s, %(appointment_date)s, %(start_time)s,
                            %(status)s, %(start_at)s, %(end_at)s, %(total_price)s, %(total_duration_minutes)s,
                            %(customer_first_name)s, %(customer_last_name)s, %(customer_phone)s,
                            %(idempotency_key)s, %(created_at)s, %(updated_at)s
                        )
                        """,
                        r,
                    )
                    imported += 1

                # Сбрасываем счетчики автоинкремента ID
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence('appointments','appointment_id'), (SELECT COALESCE(MAX(appointment_id),1) FROM appointments));"
                )
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence('clients','client_id'), (SELECT COALESCE(MAX(client_id),1) FROM clients));"
                )

                print(f"Успех! Перенесено записей: {imported}, пропущено: {skipped}.")
    finally:
        conn.close()

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data-1791900799410.csv")