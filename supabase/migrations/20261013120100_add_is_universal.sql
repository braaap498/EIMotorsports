-- Universal-fit flag: true = fits many vehicles (not tied to one make/model/year).
-- Defaults to false so every existing product keeps behaving as "fitment-specific"
-- until we bulk-set the universal ones from titles and item specifics.

alter table products
  add column if not exists is_universal boolean not null default false;

comment on column products.is_universal is
  'True when the part is universal-fit rather than specific to a make/model/year.';
