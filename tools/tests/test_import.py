from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import import_from_sqlserver as imp

NY = ZoneInfo("America/New_York")
d = datetime


def legacy():
    """A deliberately messy slice of the old database."""
    return {
        "Categories": [
            {"CategoryID": 1, "CategoryName": "Shock Parts", "EbayStoreCatID": "999", "CreatedDate": d(2024, 1, 1)},
            {"CategoryID": 2, "CategoryName": "Fork  Parts ", "CreatedDate": None},
        ],
        "VehicleTypes": [{"VehicleTypeID": 1, "TypeName": "Dirt Bike"}],
        "VehicleMakes": [{"MakeID": 1, "MakeName": "Yamaha"}],
        "VehicleModels": [
            {"ModelID": 1, "MakeID": 1, "VehicleTypeID": 1, "ModelName": "YZ450F"},
            {"ModelID": 2, "MakeID": 1, "VehicleTypeID": 1, "ModelName": "YZ250F"},
        ],
        "PartManufacturers": [{"PartManufacturerID": 1, "Name": "Showa"}],
        "Products": [
            {"ProductID": 10, "SKU": "SH-001", "Title": "Rear shock rebuild kit", "CategoryID": 1,
             "PartManufacturerID": 1, "ModelID": 1, "FitmentYearStart": 2019, "FitmentYearEnd": 2019,
             "Barcode": "", "QuantityOnHand": 5, "ListPrice": Decimal("49.99"), "IsActive": True,
             "EbayItemID": "123456", "ImageUrl": "https://i.ebayimg.com/a.jpg",
             "Description": "<p>Great kit</p><script>alert(1)</script>"
                            "<a href='https://x.com' onclick='bad()'>link</a>"
                            "<a href='javascript:evil()'>x</a><style>p{}</style>",
             "CreatedDate": d(2024, 1, 5), "ModifiedDate": d(2024, 2, 1)},
            {"ProductID": 11, "SKU": "SH-002", "Title": "Rear shock rebuild kit", "CategoryID": 1,
             "ModelID": 2, "FitmentYearStart": 2018, "FitmentYearEnd": 2020, "Barcode": "",
             "QuantityOnHand": 2, "ListPrice": Decimal("59.00"), "IsActive": False, "EbayItemID": "888"},
            {"ProductID": 12, "SKU": "FK-003", "Title": "Fork seal", "CategoryID": 2,
             "ImageUrl": "https://i.ebayimg.com/c.jpg", "QuantityOnHand": 0, "IsActive": True,
             "Description": "   ", "EbayItemID": "123456"},          # duplicate of product 10's item
            {"ProductID": 13, "SKU": "   ", "Title": "No sku", "QuantityOnHand": 0},
            {"ProductID": 14, "SKU": "NY-004", "Title": "Needs year", "ModelID": 1, "QuantityOnHand": 0,
             "IsActive": True, "EbayItemID": "EBAY-JUNK"},
        ],
        "ProductPhotos": [
            {"ProductPhotoID": 1, "ProductID": 10, "PhotoUrl": "https://i.ebayimg.com/a.jpg", "IsPrimary": True, "SortOrder": 0},
            {"ProductPhotoID": 2, "ProductID": 10, "PhotoUrl": "https://i.ebayimg.com/b.jpg", "IsPrimary": True, "SortOrder": 1},
            {"ProductPhotoID": 3, "ProductID": 10, "PhotoUrl": "https://i.ebayimg.com/a.jpg", "IsPrimary": False, "SortOrder": 2},
        ],
        "ProductItemSpecifics": [
            {"ProductItemSpecificID": 1, "ProductID": 10, "SpecificName": "Warranty", "SpecificValue": "1 year", "SortOrder": 0},
            {"ProductItemSpecificID": 2, "ProductID": 10, "SpecificName": "", "SpecificValue": "orphan", "SortOrder": 1},
        ],
        "ProductCompatibility": [
            {"ProductCompatibilityID": 1, "ProductID": 10, "Year": 2019, "Make": "YAMAHA", "Model": "yz450f", "Submodel": "", "Notes": "fits all"},
            {"ProductCompatibilityID": 2, "ProductID": 10, "Year": 2019, "Make": "Yamaha", "Model": "YZ450F", "Submodel": None, "Notes": None},
            {"ProductCompatibilityID": 3, "ProductID": 10, "Year": 2020, "Make": "Husqvarna", "Model": "FC450", "Submodel": "Rally", "Notes": None},
            {"ProductCompatibilityID": 4, "ProductID": 10, "Year": None, "Make": "Yamaha", "Model": "YZ450F"},
        ],
        "InventoryTransactions": [
            {"TransactionID": 100, "ProductID": 10, "TransactionType": "Receive", "QuantityChange": 5,
             "Reference": "PO-1", "TransactionDate": d(2024, 7, 1, 12, 0), "CreatedBy": "Ian"},
            {"TransactionID": 101, "ProductID": 11, "TransactionType": "Receive", "QuantityChange": 3, "TransactionDate": d(2024, 1, 15, 9, 0)},
            {"TransactionID": 102, "ProductID": 11, "TransactionType": "Sale", "QuantityChange": -1, "TransactionDate": d(2024, 2, 1, 9, 0)},
            {"TransactionID": 103, "ProductID": 11, "TransactionType": "Bogus", "QuantityChange": 1},
        ],
        "Users": [{"UserID": 1, "DisplayName": "Ian"}],
        "Vendors": [{"VendorID": 1, "VendorName": "Showa Parts Co", "ContactInfo": "x", "IsActive": True}],
        "PurchaseOrders": [{"PurchaseOrderID": 1, "VendorID": 1, "OrderDate": d(2024, 6, 20), "ReceivedDate": d(2024, 7, 1),
                            "ReferenceNumber": "PO-1", "CreatedByUserID": 1}],
        "PurchaseOrderLines": [{"PurchaseOrderLineID": 1, "PurchaseOrderID": 1, "ProductID": 10,
                                "QuantityOrdered": 5, "QuantityReceived": 5, "UnitCost": Decimal("20.00")}],
        "EbaySettings": [{"EbaySettingsID": 1, "SellerUsername": "ei_motorsports", "CategoryIds": "6000, 6001",
                          "SyncEnabled": True}],
        "SalesChannels": [{"SalesChannelID": 1, "ChannelName": "eBay"}, {"SalesChannelID": 2, "ChannelName": "Etsy"}],
        "SalesTransactions": [
            {"SalesTransactionID": 1, "ProductID": 11, "SalesChannelID": 1, "QuantitySold": 1, "UnitSalePrice": Decimal("59.00"),
             "SaleDate": d(2024, 2, 1, 9, 0), "InventoryTransactionID": 102, "ShipToState": "NC", "QBTxnID": "QB1"},
            {"SalesTransactionID": 2, "ProductID": 11, "SalesChannelID": 2, "QuantitySold": 0, "UnitSalePrice": Decimal("59.00")},
        ],
    }


def run(conn, **kw):
    kw.setdefault("replace", True)
    return imp.run_import(imp.DictSource(legacy()), conn, tz=NY, **kw)


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def test_dry_run_saves_nothing(conn, db_url):
    run(conn)
    conn.rollback()
    import psycopg
    with psycopg.connect(db_url) as other:
        assert other.execute("select count(*) from products").fetchone()[0] == 0


def test_row_counts_and_skips(conn):
    rep = run(conn)
    c = rep.dest_counts
    assert c["categories"] == 2
    assert c["products"] == 4                      # blank-SKU product skipped
    assert c["inventory_transactions"] == 3        # 'Bogus' type skipped
    assert c["sales_transactions"] == 1            # quantity-0 row skipped
    assert any("no SKU" in r or "missing SKU" in r for _, r in rep.skipped["products"])
    assert any("quantity sold" in r for _, r in rep.skipped["sales_transactions"])
    assert any("unknown type" in r for _, r in rep.skipped["inventory_transactions"])
    assert c["purchase_order_lines"] == 1


def test_fitment_is_normalized_and_deduplicated(conn):
    rep = run(conn)
    # P10: 2019 YZ450F (3 spellings of the same thing) + 2020 Husqvarna FC450 Rally
    # P11: model YZ250F years 2018-2020 -> 3 vehicles.  P14: model but no years -> none.
    assert rep.dest_counts["vehicles"] == 5
    assert rep.dest_counts["product_fitment"] == 5
    makes = {r[0] for r in q(conn, "select name from vehicle_makes")}
    assert makes == {"Yamaha", "Husqvarna"}        # 'YAMAHA' folded into 'Yamaha'
    assert q(conn, "select notes from product_fitment where product_id=10 and notes is not null") == [("fits all",)]
    assert any("NO year range" in n and "NY-004" in n for n in rep.notes)
    assert any("Husqvarna" in n for n in rep.notes)


def test_storefront_year_make_model_query(conn):
    run(conn, publish_active=True)
    rows = q(conn, """select p.sku from products p
        join product_fitment f on f.product_id=p.id join vehicles v on v.id=f.vehicle_id
        join vehicle_models m on m.id=v.model_id join vehicle_makes k on k.id=m.make_id
        where k.slug='yamaha' and m.slug='yz450f' and v.year=2019 and p.is_published and p.is_active""")
    assert rows == [("SH-001",)]


def test_slugs_unique_and_clean(conn):
    run(conn)
    slugs = [r[0] for r in q(conn, "select slug from products")]
    assert len(slugs) == len(set(slugs)) == 4
    assert "rear-shock-rebuild-kit-sh-001" in slugs and "rear-shock-rebuild-kit-sh-002" in slugs
    assert q(conn, "select name, slug from categories where id=2") == [("Fork Parts", "fork-parts")]


def test_descriptions_are_sanitized(conn):
    run(conn)
    desc = q(conn, "select description from products where id=10")[0][0]
    assert "<script" not in desc and "alert(1)" not in desc and "onclick" not in desc
    assert "javascript:" not in desc and "p{}" not in desc
    assert "Great kit" in desc and 'href="https://x.com"' in desc
    assert q(conn, "select description from products where id=12")[0][0] is None   # blank -> NULL


def test_exactly_one_primary_photo_per_product(conn):
    run(conn)
    rows = q(conn, "select product_id, count(*) filter (where is_primary), count(*) from product_photos group by 1 order by 1")
    assert rows == [(10, 1, 2), (12, 1, 1)]        # duplicate URL collapsed; ImageUrl-only product gets one
    assert q(conn, "select storage_path from product_photos where product_id=10 limit 1") == [(None,)]
    assert q(conn, "select source_url from product_photos where product_id=10 and is_primary") == [("https://i.ebayimg.com/a.jpg",)]


def test_blank_barcodes_do_not_collide(conn):
    run(conn)
    assert q(conn, "select count(*) from products where barcode is null")[0][0] == 4


def test_ebay_ids_kept_as_background_cross_references(conn):
    rep = run(conn)
    # core catalog stays marketplace-free
    cols = {r[0] for r in q(conn, "select column_name from information_schema.columns where table_name='products'")}
    assert not any("ebay" in c for c in cols)
    # item numbers live only in channel_listings
    rows = q(conn, """select p.sku, c.name, l.external_id, l.external_url, l.status
                      from channel_listings l join products p on p.id=l.product_id
                      join sales_channels c on c.id=l.channel_id order by p.sku""")
    assert rows == [
        ("NY-004", "eBay", "EBAY-JUNK", None, "active"),                       # non-numeric id: no link built
        ("SH-001", "eBay", "123456", "https://www.ebay.com/itm/123456", "active"),
        ("SH-002", "eBay", "888", "https://www.ebay.com/itm/888", "ended"),    # inactive product -> ended
    ]
    # the duplicate item number on FK-003 is reported, not silently dropped or crashed on
    assert any("already linked" in why for _, why in rep.skipped["channel_listings"])
    assert any("inferred" in n for n in rep.notes)
    # store category id and seller settings
    assert q(conn, "select category_id, external_id from channel_category_refs") == [(1, "999")]
    settings = q(conn, "select settings from integration_settings where provider='ebay'")[0][0]
    assert settings == {"seller_username": "ei_motorsports", "category_ids": ["6000", "6001"]}
    assert rep.dest_counts["channel_listings"] == 3 and rep.dest_counts["channel_category_refs"] == 1


def test_deleting_the_ebay_link_leaves_the_catalog_intact(conn):
    run(conn)
    conn.execute("delete from channel_listings")
    conn.execute("delete from channel_category_refs")
    assert q(conn, "select count(*) from products")[0][0] == 4
    assert q(conn, "select count(*) from categories")[0][0] == 2


def test_timestamps_become_real_utc_instants(conn):
    run(conn)
    # 2024-07-01 12:00 New York (EDT, UTC-4) == 16:00 UTC; 2024-01-15 09:00 EST (UTC-5) == 14:00 UTC
    got = dict(q(conn, "select id, created_at at time zone 'UTC' from inventory_transactions"))
    assert got[100] == d(2024, 7, 1, 16, 0)
    assert got[101] == d(2024, 1, 15, 14, 0)


def test_legacy_user_names_preserved(conn):
    run(conn)
    assert q(conn, "select legacy_created_by from inventory_transactions where id=100") == [("Ian",)]
    assert q(conn, "select legacy_created_by from purchase_orders where id=1") == [("Ian",)]


def test_channels_reuse_existing_and_add_new(conn):
    run(conn)
    names = sorted(r[0] for r in q(conn, "select name from sales_channels"))
    assert names == ["Etsy", "In Person", "Website", "eBay"]       # eBay reused, not duplicated
    assert q(conn, "select c.name from sales_transactions s join sales_channels c on c.id=s.channel_id")[0][0] == "eBay"


def test_ids_preserved_and_sequences_advanced(conn):
    run(conn)
    assert q(conn, "select id from products order by id") == [(10,), (11,), (12,), (14,)]
    new_id = q(conn, "insert into products (sku, slug, title) values ('NEW','new','New') returning id")[0][0]
    assert new_id == 15                                # continues after the highest imported id
    tx = q(conn, "insert into inventory_transactions (product_id, transaction_type, quantity_change) values (10,'Adjustment',1) returning id")[0][0]
    assert tx == 103                                   # ledger ids 100-102 were imported


def test_publish_flag(conn):
    run(conn)
    assert q(conn, "select count(*) from products where is_published")[0][0] == 0   # safe default
    run(conn, publish_active=True)
    assert sorted(r[0] for r in q(conn, "select id from products where is_published")) == [10, 12, 14]


def test_refuses_to_run_twice_without_replace(conn):
    run(conn)
    with pytest.raises(imp.ImportAbort):
        imp.run_import(imp.DictSource(legacy()), conn, tz=NY, replace=False)


def test_replace_is_repeatable(conn):
    a = run(conn).dest_counts
    b = run(conn).dest_counts
    assert a == b


def test_checks_report(conn):
    rep = run(conn)
    assert rep.checks["total units on hand (source vs imported)"] == "7 vs 7"
    assert rep.checks["products with no fitment"] == 2          # fork seal + needs-year
    assert "IMPORT REPORT" in rep.render()


def test_sanitizer_unit():
    assert imp.sanitize_html("   ") is None
    assert imp.sanitize_html("<p> </p>") is None
    assert imp.sanitize_html("<img src=x onerror=alert(1)>text") == "text"
    assert imp.slugify("Ünïcode & Spëcial!! Chars") == "unicode-special-chars"


def test_cli_dry_run_then_commit_then_guard(db_url, monkeypatch, capsys):
    """The real entry point: default is a dry run; --commit saves; a second run is refused."""
    import psycopg
    monkeypatch.setattr(imp, "MssqlSource", lambda conn_str: imp.DictSource(legacy()))

    def count():
        with psycopg.connect(db_url) as c:
            return c.execute("select count(*) from products").fetchone()[0]

    try:
        imp.main(["--database-url", db_url, "--replace"])
        out = capsys.readouterr().out
        assert "DRY RUN" in out and "IMPORT REPORT" in out
        assert count() == 0

        imp.main(["--database-url", db_url, "--commit"])
        assert "COMMITTED" in capsys.readouterr().out
        assert count() == 4

        with pytest.raises(SystemExit) as e:
            imp.main(["--database-url", db_url, "--commit"])
        assert "already has products" in str(e.value)
        assert count() == 4                              # refused run changed nothing
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute("truncate " + ", ".join(imp.WIPE_TABLES) + " restart identity cascade")
