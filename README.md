# Toror Technology and Innovations Ltd

A Flask website with a login-first public flow, private project uploads, a hidden share-link vault for videos, and a chat inbox with live polling.

## Main flow
- `/login` is the first public page.
- Users request a login email and then verify the link sent through the company email account.
- After login, they land on `/portal`.
- The hidden private route for the workspace is `/xtspolsjhulupjoppsuplmkzcodup`.

## Environment variables
- `SECRET_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` (or `RENDER_ENV_PASSWORD`)
- `ADMIN_EMAIL`
- `ADMIN_DEVELOPMENT_OPEN` (`1` for local/no-password admin access, `0` for hosted credentials; default is `0`)
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM`
- `PRIMARY_EMAIL`
- `TOROR_DB_PATH`
- `TOROR_DATA_DIR`

## Run locally
```bash
pip install -r requirements.txt
python app.py
```

## Notes
- Logo upload is supported from the private settings page.
- Projects can include PDFs, spreadsheets, images, documents, and videos.
- Vault videos are shared by private token links like `/v/<token>`.
- The site uses SQLite, a PWA shell, and cached static assets.
