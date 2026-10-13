-- =====================================================================
-- 0003: background cross-references for outside sales channels
--
-- Product links to eBay (item numbers) already live in channel_listings.
-- This adds the same idea for categories, e.g. an eBay Store category id,
-- so listings can be pushed to the right place later. Optional: nothing
-- on the website reads this table.
-- =====================================================================
create table public.channel_category_refs (
  category_id  bigint not null references public.categories(id) on delete cascade,
  channel_id   bigint not null references public.sales_channels(id),
  external_id  text   not null,
  primary key (category_id, channel_id)
);

alter table public.channel_category_refs enable row level security;
revoke all on public.channel_category_refs from anon, authenticated;
