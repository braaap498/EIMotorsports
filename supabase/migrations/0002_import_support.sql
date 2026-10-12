-- =====================================================================
-- 0002: support for importing the legacy desktop-app data
--
--  * Photos can be imported before they are copied into our own storage,
--    so storage_path may be NULL as long as a source_url exists.
--    (Legacy photo links point at eBay's servers and will stop working
--    once those listings end - a follow-up step copies them into our
--    storage and fills in storage_path.)
--  * The old app tracked "who did this" as a free-text display name, not
--    a login. Keep that history in legacy_created_by.
-- =====================================================================

alter table public.product_photos alter column storage_path drop not null;
alter table public.product_photos
  add constraint product_photos_has_location
  check (storage_path is not null or source_url is not null);

alter table public.inventory_transactions add column legacy_created_by text;
alter table public.purchase_orders        add column legacy_created_by text;
