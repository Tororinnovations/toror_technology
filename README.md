# Toror Technology Company Ltd — public corporate site + private administration

The public website is intentionally open: visitors do not register, log in, or create an account. The public experience includes the company home page, who-we-are and history sections, services, selected work, FAQs, contact/enquiries, privacy, terms, certificate verification, a homepage QR code, and mobile navigation.

The master visual system is **near-white sky-blue content + light maroon header/footer/chrome + black text + dark-red buttons with white text**. No agricultural dark-green theme is used.

## Private administration

Administrator access is intentionally separate from the public site:

- Private entry path: `/promise212324`
- Render variable: `ADMIN_NAME`
- Render variable: `ADMIN_PASSWORD`
- Render variable: `SECRET_KEY`

The credential reader tolerates accidental surrounding quotes and also recognises `ADMIN_USERNAME` / `ADMIN_USER` and `ADMIN_PASS` as compatibility fallbacks. Keep the intended variables in Render and do not put secrets in templates or source control.

## Certificates

The certificate studio generates a professional Toror certificate for a software customer or business recipient. Each certificate records:

- Recipient name
- Business / organisation
- Software purchased
- Award title
- Issuer name
- Issuer title
- Award date
- Recognition note
- Unique Toror serial number
- HMAC-based authenticity code
- QR verification link
- Optional electronic issuer signature image

Issuer settings are changeable over time. Newly generated certificates snapshot the issuer information; changing the CEO/issuer later therefore does not rewrite the historical issuer details on already-issued certificates.

The certificate signature is calculated from the certificate record and **does not include the hostname**, so changing the application's domain does not invalidate the authenticity signature. `CERTIFICATE_BASE_URL` or the admin **Certificate base URL** setting can be used when the site is moved and new certificates should point to a preferred canonical domain.

## Logo and identity

Admin → Site settings allows the official Toror logo to be uploaded. The current logo is used by the public site, admin interface, favicon redirect, and installed-app manifest.

## Backups and restore

Admin → Site settings contains:

- **Download database** — a consistent SQLite snapshot using SQLite's backup API.
- **Download full backup** — database plus uploaded assets such as logos, issuer signatures, certificates, project assets and vault files.
- **Restore backup** — accepts a Toror full-backup ZIP or a validated SQLite database. A pre-restore database snapshot is retained before replacement.

For sensitive records, keep exported backups outside the Render service as well. The local database alone does not contain uploaded files, so the full-backup ZIP is the appropriate complete system backup.

## Public project links

The portfolio is curated around:

- `https://oedge.onrender.com/`
- `https://denmart.co.ke/`
- `https://otravel-bleg.onrender.com/`
- `https://prime-1-rd0g.onrender.com/`

## Environment

Typical deployment variables:

- `SECRET_KEY`
- `ADMIN_NAME`
- `ADMIN_PASSWORD`
- `PRIMARY_EMAIL` (optional)
- `PRIMARY_PHONE` (optional)
- `CERTIFICATE_BASE_URL` (optional; otherwise the current domain is used)
- `TOROR_DATA_DIR` (optional)
- `TOROR_DB_PATH` (optional)

## Run

```bash
gunicorn app:app
```

SQLite is used by default in `data/toror.db`. The existing database is retained and upgraded automatically for new certificate issuer and backup-related functionality. Public CSS, JavaScript and service-worker assets are versioned so mobile browsers receive the latest UI instead of an old cached colour scheme.
