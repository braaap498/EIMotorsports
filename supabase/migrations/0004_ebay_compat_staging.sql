-- =====================================================================
-- 0004: raw staging area for eBay "compatible vehicles" lists
--
-- Holds what eBay returned for each listing, exactly as received and
-- uninterpreted. Nothing on the website reads it. It exists so that when
-- the year/make/model database is ready, these lists (and the vehicle
-- details in the titles) can be matched against it.
--
--   ok          eBay returned a compatibility list
--   count_only  eBay says the listing HAS compatibilities but did not return
--               the list (e.g. listed "by specification"); a different eBay
--               call is needed for those
--   no_list     listing fetched fine, no compatibility information on it
--   not_found   listing ended / not accessible
--   error       any other failure; retried on the next run
-- =====================================================================
create table public.ebay_compat_raw (
  channel_listing_id  bigint primary key references public.channel_listings(id) on delete cascade,
  ebay_item_id        text   not null,
  status              text   not null check (status in ('ok','count_only','no_list','not_found','error')),
  compat_count        integer,
  compat_list         jsonb,
  error_code          text,
  error_message       text,
  fetched_at          timestamptz not null default now()
);
create index ebay_compat_raw_status_idx on public.ebay_compat_raw (status);

alter table public.ebay_compat_raw enable row level security;
revoke all on public.ebay_compat_raw from anon, authenticated;
