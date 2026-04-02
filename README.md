# Aoe4IT Faceit Style Site

Flask based Age of Empires IV competitive site with:
- home, play, players, ladders, tournaments, news, premium
- account system with password recovery
- queue into practice rooms
- map draft, civ bans, hidden picks, secret snipes
- tournament registration, bracket generation, admin tools
- hidden draft lab for testing

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5055`

## Render

This project includes `render.yaml`.
The app stores its SQLite database on the mounted Render disk at `/var/data/aoe4it.db`.

Recommended environment variables:
- `APP_BASE_URL`
- `ADMIN_EMAILS`
- `INVITE_CODE` optional
- `RESEND_API_KEY` and `MAIL_FROM` for email
or SMTP variables:
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_USE_TLS`


## Registration and email behavior

- New account registrations are saved in SQLite immediately.
- Player profile creation is saved and linked back to the account.
- Tournament registrations are saved in the `registrations` table.
- Every email attempt is saved in `email_delivery_log`, including `sent`, `failed`, and `skipped` states.
- Key account actions are saved in `audit_log` for admin review.
- The admin page now shows recent signups, saved activity, and saved email attempts.

Useful env vars for persistence and mail:
- `DATABASE_PATH` optional override for SQLite location
- `APP_BASE_URL` used in emails
- `MAIL_FROM`
- `RESEND_API_KEY` for Resend delivery
- or `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`


## Important local run note
Always extract the ZIP first. Do not run `app.py` from inside the ZIP preview, because Flask will not be able to see the `templates/` and `static/` folders.

Use `run_local.bat` after extracting, or open a terminal in the extracted folder and run `python app.py`.


## Phase 1 security hardening

This build adds:
- CSRF protection for all POST forms
- Rate limiting on login, register, password reset, queue, tournament, admin, and draft actions
- Hardened session cookies in production
- Basic security headers

Recommended production env vars:
- `SECRET_KEY` must be set and strong
- `APP_ENV=production`
- `APP_BASE_URL=https://your-domain.example`
- `SESSION_COOKIE_SAMESITE=Lax`
- `ENABLE_SHADOW_LAB=false` unless you explicitly want it enabled

Notes:
- `SESSION_COOKIE_SECURE` is turned on automatically in production
- the app now refuses to boot in production if `SECRET_KEY` is left on the development default
- rate limiting is stored in the app database for this build


## Phase 2 deployment readiness

Decision for this build:
- **SQLite stays in place for staging and private beta**
- **Postgres is deferred until after live staging QA**

Why this decision:
- the current app is already stable on a mounted Render disk
- Phase 2 is about making the staging deploy honest and reliable, not pretending the app already has a full Postgres migration
- once staging is proven in real traffic, the database move can happen with fewer unknowns

What this Phase 2 build adds:
- `/healthz` read health endpoint
- `/readyz` readiness endpoint used by Render
- `/ops/runtime` admin JSON diagnostics endpoint
- runtime configuration checks for:
  - APP_ENV validity
  - HTTPS APP_BASE_URL on deployed environments
  - writable database directory
  - missing admin emails
  - email configuration mismatches
- SQLite indexes for the busiest tables
- Render blueprint tuned for **staging** deploys through GitHub

Recommended Render flow:
1. Push this build to GitHub
2. Create the Render service from `render.yaml`
3. Set `APP_BASE_URL` to the real Render URL or custom domain
4. Add `ADMIN_EMAILS`
5. Add email credentials only if you want live delivery on staging
6. Deploy and confirm `/readyz` returns `ok: true`
7. Then run Phase 3 live QA

When to move to Postgres:
- move after the staging deploy is healthy and the core flows are verified live
- move before a wider public launch, especially if you expect multiple simultaneous users, more tournaments, or premium features
