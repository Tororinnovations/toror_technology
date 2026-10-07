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

## Public projects and Apps & Sites Store

The public portfolio no longer contains hard-coded Toror website links. Add websites through **Admin → Apps & Sites Store** so each uploaded site can have its own name, preview/summary, description, price, payment switch, premium switch, release history and access instructions.

The home page and store show a compact product preview/summary; the full website opens only through its product access flow.

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

## Apps & Sites Store

Admin → **Apps & Sites Store** now provides a small first-party product store for Toror APKs and uploaded static website packages. Product records, release history, purchase orders, gateway receipts and accounting entries are stored in SQLite. Uploaded APKs and website packages live under `static/uploads/store/`, so the existing **full system backup** includes them.

Paid checkout requires the buyer name, the paying phone number and the exact amount. The listener/API gateway can POST M-PESA receipt data to the endpoint displayed inside the store admin. Automatic approval is only performed when one pending order matches on normalized name, normalized Kenyan phone number, exact amount and the configured time window. Unmatched and ambiguous receipts remain recorded for manual review.
APK download analytics are retained in SQLite as well: the admin can see total downloads, unique APK downloaders, the APK/version downloaded, buyer name, phone/email, time, and whether each download was an initial download, update, or re-download. This measures downloads, not guaranteed installations; Android installation completion is controlled by the device/app.

### Listener endpoint

The canonical receiver is `/api/gateway/mpesa` and `/api/gateway/messages` is an alias. Configure the shared secret in Admin → Apps & Sites Store and send it as `X-Toror-Gateway-Key` or `Authorization: Bearer ...`. A receipt POST may include structured fields such as `payer_name`, `payer_phone`, `amount`, `transaction_code`, `gateway_id`, `received_at`, `sim_device`, `sender`, `delivery`, and `raw_message`; the server can also extract common M-PESA fields from the raw message.

### Release updates
The same approved APK access link always serves the product's current release. Publishing a newer version changes the current release pointer without invalidating existing approved access. The update metadata endpoint is cache-disabled so clients can see newly published releases promptly.

Re-uploading an APK or website never deletes the currently live release first. A new release is validated, stored as its own version, and only then made current. Existing approved access links follow the latest release. The API endpoint `/api/store/apk/<slug>/update-check` exposes the current version and, for an entitled buyer, a latest-download URL. Normal Android devices still require the installed app to check this endpoint and follow Android's installation/update rules; a normal website server cannot silently install an APK on every phone.
