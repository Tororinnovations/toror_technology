# Toror Technology — Website and Apps Store

Public company website and digital products catalogue built with Flask. The site includes company information, services, projects, contact forms, certificate verification, and an Apps & Sites catalogue with release management.

## Deployment

- Start command: `gunicorn app:app`
- Python dependencies: `requirements.txt`
- Set a strong `SECRET_KEY` in the hosting provider's environment settings.
- Configure the administrator credentials only in the hosting provider's environment settings. Never commit credentials, `.env` files, database backups, customer exports, or APK releases to this repository.
- Keep this repository **private** if you do not want application source code, URL routes, or implementation details to be publicly visible. Removing route names from this README does not hide them in a public source repository.

## Durable APK hosting on Render Free

Render's free service filesystem is ephemeral. APK files uploaded only to the service's local disk can disappear after deploys, restarts, or instance replacement. Cache headers do not provide permanent server-side storage.

For durable release storage, create a private Cloudflare R2 bucket and set these environment variables in Render:

- `R2_ACCOUNT_ID`
- `R2_BUCKET_NAME`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

With these configured, **new APK uploads** are stored as private R2 objects and served through the application's existing download authorization flow. Existing releases on the temporary filesystem must be uploaded again after configuration or migrated to the bucket. Do not make the bucket public and do not put R2 secrets in GitHub. Without R2 configuration, the app retains local-storage compatibility, but local uploads are not durable on Render Free.

## Search indexing and custom domain

After `apps.trorinovationstech.co.ke` is attached and verified in Render, set `CANONICAL_BASE_URL` to `https://apps.trorinovationstech.co.ke`. The app provides `/robots.txt` and `/sitemap.xml`, and the sitemap includes active catalogue product pages. Submit `https://apps.trorinovationstech.co.ke/sitemap.xml` in Google Search Console once DNS and HTTPS work. Search visibility is not instant or guaranteed; useful product descriptions, page titles, original screenshots, and legitimate links also matter.

## Data and backups

SQLite and local uploads are not a substitute for durable external storage on a free ephemeral host. Export backups regularly and store them somewhere separate from the running service. Keep all backups and customer information out of Git.
