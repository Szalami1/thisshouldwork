# Render staging QA checklist

Use this after deploying the Phase 3 build to Render.

## 1. Confirm the service is healthy
- Open `/healthz`
- Open `/readyz`
- Open `/ops/runtime` while logged in as an admin

## 2. Run the smoke script against the deployed URL
```bash
python smoke_test_render.py https://your-render-url.onrender.com
```

## 3. Manual auth checks
- Register a new account
- Confirm the account lands on Players
- Create a player profile
- Log out
- Log back in
- Open Settings
- Trigger Forgot Password

## 4. Manual competitive flow checks
- Join BO1 queue
- Leave queue
- Create a tournament as admin
- Register a player into that tournament
- Start the bracket
- Open any generated match
- Submit a score
- Confirm the match state updates

## 5. Email checks
- Verify welcome email behavior
- Verify forgot-password email behavior
- Verify tournament registration email behavior
- Check `email_delivery_log` through Admin or the database

## 6. Production-only things to watch for
- Cookie/session issues over HTTPS
- Broken absolute links from `APP_BASE_URL`
- Static files not loading through Render
- Database writes failing because the disk path is wrong
- Any 404 or 500 pages unexpectedly appearing during normal flows

## 7. Before opening wider
- Replace the secret key if still on a default value
- Confirm `ENABLE_SHADOW_LAB=false`
- Confirm admin emails are correct
- Confirm mail provider config is complete
