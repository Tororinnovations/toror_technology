# Toror Technology Company Ltd — public corporate site + private administration

The public website is intentionally open: visitors do not register, log in, or create an account. The public experience includes the company home page, who-we-are and history sections, services, selected work, FAQs, contact/enquiries, privacy, terms, and certificate verification.

## Private administration

Administrator access is intentionally separate from the public site:

- Private entry path: `/promise212324`
- Render variable: `ADMIN_NAME`
- Render variable: `ADMIN_PASSWORD`
- Render variable: `SECRET_KEY`

Keep these values only in Render environment variables; do not place them in templates or source control.

The admin workspace retains the existing projects, vault, contacts, messages, users, logo, and site settings features and adds a certificate studio.

## Certificates

The certificate studio generates a PDF certificate for a software customer or business recipient. Each certificate gets a unique Toror serial number, a tamper-evident HMAC signature, and a QR code that opens the public verification page. Verification checks the signed certificate record stored by the site.

The system makes imitation substantially harder and provides an independent verification path; no visual document can be guaranteed to be literally impossible to counterfeit.

## Environment

Typical deployment variables:

- `SECRET_KEY`
- `ADMIN_NAME`
- `ADMIN_PASSWORD`
- `PRIMARY_EMAIL` (optional)
- `PRIMARY_PHONE` (optional)
- `TOROR_DATA_DIR` (optional)
- `TOROR_DB_PATH` (optional)

## Run

```bash
gunicorn app:app
```

SQLite is used by default in `data/toror.db`. The existing database is retained and upgraded automatically for the new enquiry and certificate fields.
