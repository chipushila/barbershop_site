# AIR Barbershop booking (Flask + PostgreSQL)

## Deploy on Render
1. Push this folder to a GitHub repository (`.env`, `*.backup`, and the CSV are git-ignored: they hold secrets / customer data).
2. Render dashboard -> **New -> Blueprint** -> pick the repo. `render.yaml` creates the web service and the database.
3. On first start the app applies `schema.sql` automatically (idempotent) and seeds services, barbers and working hours.
4. Import existing bookings from your machine, using the database's **External Database URL** from the Render dashboard:
   `DATABASE_URL='<external url>' python import_appointments.py data-1791900799410.csv`
5. Open `https://<service>.onrender.com/healthz` -> `{"ok": true}`.

## Environment variables
`DATABASE_URL` (set by Render), `BARBERSHOP_TZ` (default `Asia/Astana`), `ADMIN_TOKEN` (enables the admin API, sent as `X-Admin-Token`), `FLASK_DEBUG` (keep `0`), `SHOP_WIDE_BLOCKING`, `SLOT_STEP_MINUTES`, `DAYS_AHEAD`.

## Local run
```
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
gunicorn backend:app
TEST_DATABASE_URL=postgresql://... pytest -q   # throwaway DB only
```
