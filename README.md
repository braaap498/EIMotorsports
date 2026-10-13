# E&I Motorsports

Website-first parts retailer: public storefront + back-office, one API, one database.
Marketplaces (eBay included) are optional sales channels, never part of the core.

```
supabase/migrations/   Postgres schema (source of truth)
backend/               FastAPI API  (/store public, /admin staff-only)
tools/                 one-time import of the old desktop-app database
render.yaml            Render blueprint for the API
storefront/            Next.js public site        (step 2)
admin/                 Flutter back-office        (step 3)
```

## 1. Supabase (use a NEW project, separate from FCM)
FCM is a multi-tenant SaaS with real customers; keep this store's data, backups,
keys, and blast radius separate.

1. Create the project. Pick a region close to your Render region.
2. SQL Editor -> paste `supabase/migrations/0001_core_schema.sql` -> Run.
   Afterward, Table Editor should show 20 tables, each marked "RLS enabled".
3. Project Settings -> Database -> Connection string -> **Session pooler**.
   Change the scheme to `postgresql+psycopg://` and use it as `DATABASE_URL`.
   (Render can't reach the IPv6-only direct host.)
4. Authentication -> Users -> Add user (your email, auto-confirm).
5. Make yourself the owner (SQL Editor):
   ```sql
   update auth.users
   set raw_app_meta_data = coalesce(raw_app_meta_data,'{}'::jsonb) || '{"role":"owner"}'
   where email = 'YOU@EXAMPLE.COM';

   insert into public.staff_profiles (user_id, display_name, role)
   select id, 'Your Name', 'owner' from auth.users where email = 'YOU@EXAMPLE.COM';
   ```
   Roles live in `app_metadata`, which users cannot edit themselves. Customers
   who sign up later get no role and are refused by every `/admin` route.
   A role change takes effect when the user's current token expires (~1 hour).

## 2. Render
1. New -> Blueprint -> pick this repo (it reads `render.yaml`).
2. Set env vars: `DATABASE_URL`, `SUPABASE_URL`, `ALLOWED_ORIGINS`.
   `SUPABASE_JWT_SECRET` only if your project still uses the legacy shared secret
   (Project Settings -> JWT Keys). Leave it blank for asymmetric signing keys.
3. After deploy: `GET https://<service>.onrender.com/health` -> `{"status":"ok"}`

## 3. Cloudflare
Add a CNAME `api` -> your Render hostname, then add `api.eandimotorsports.com`
as a custom domain in Render. Keep the record DNS-only until Render shows the
certificate as issued, then you can turn the proxy on (SSL mode: Full strict).
`www` / apex stay free for the storefront.

## 4. Verify the whole chain
```bash
# get a token (anon/publishable key: Project Settings -> API Keys)
curl -s -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d '{"email":"YOU@EXAMPLE.COM","password":"..."}'   # copy access_token

curl -H "Authorization: Bearer $TOKEN" https://api.eandimotorsports.com/admin/me       # your role
curl -H "Authorization: Bearer $TOKEN" https://api.eandimotorsports.com/admin/ping-db  # {"database":"ok"}
curl https://api.eandimotorsports.com/admin/me                                         # 401
```

## 5. Import your existing products (one time)
Brings the old SQL Server data into the new database: products, categories,
manufacturers, fitment, photos (as links), inventory history, vendors, purchase
orders, and sales.

eBay identifiers are kept as background cross-references only, never as core data:
eBay item numbers (with a link) go to `channel_listings`, eBay Store category ids to
`channel_category_refs`, and the seller name to `integration_settings`. The website
never reads them; delete those rows and the catalog is unaffected. eBay login tokens
can't be moved (they're encrypted to the old PC), so reconnect eBay later if you want
it. QuickBooks ids are not carried.

1. Supabase -> SQL Editor -> paste and run, in order, `supabase/migrations/0002_import_support.sql`
   and then `supabase/migrations/0003_channel_cross_refs.sql`.
2. On the Windows PC that has the old database (SQL Server must be running), in PowerShell:
   ```powershell
   cd tools
   pip install -r requirements.txt
   $env:DATABASE_URL = 'postgresql+psycopg://...'     # same value you gave Render
   python import_from_sqlserver.py                    # DRY RUN: saves nothing
   ```
3. Read the report. It lists row counts, anything skipped and why, and data that
   needs a human look (products with no fitment, no photo, no price, etc.).
4. When it looks right: `python import_from_sqlserver.py --commit`

Options: `--publish-active` (otherwise everything imports UNPUBLISHED, so nothing
goes live by accident), `--replace` (wipe imported data and redo it; staff accounts
are never touched), `--mssql "<odbc string>"` if your server/database/driver differs
from the old app's defaults, `--source-tz` (default America/New_York).

Photos come over as links to eBay's servers. Those links stop working when the
listings end, so copy them into your own storage soon (a follow-up step).

Tests (need a local Postgres): `cd tools && pip install -r requirements-dev.txt && pytest`

## Local development
```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env     # fill in
pytest                   # auth tests need no database or network
uvicorn app.main:app --reload
```
