"""
E&I Motorsports API.

  /health        public, no database - used by Render's health check
  /store/*       public storefront API (added in the next step)
  /admin/*       back-office API - EVERY route requires a signed-in staff user

New admin routers go through `admin_router` so they inherit the auth
requirement automatically; there is no way to forget it per-route.
"""
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.about import admin_router as admin_about_router
from app.about import store_router as store_about_router
from app.admin_catalog import router as admin_catalog_router
from app.admin_cleanup import router as admin_cleanup_router
from app.admin_ebay_compat import router as admin_ebay_compat_router
from app.admin_ebay_fitment import router as admin_ebay_fitment_router
from app.admin_ebay_import import router as admin_ebay_import_router
from app.admin_ebay_new import router as admin_ebay_new_router
from app.admin_ebay_listings import router as admin_ebay_listings_router
from app.admin_ebay_listings import start_status_sync_daily
from app.ebay_oauth import router as ebay_oauth_router
from app.ebay_copy import router as ebay_copy_router
from app.ebay_edit import router as ebay_edit_router
from app.ebay_edit import start_zero_keeper
from app.ebay_sales import public_router as ebay_sales_public_router
from app.ebay_sales import router as ebay_sales_router
from app.ebay_sales import ship_router as ebay_ship_router
from app.ebay_sales import start_background as start_ebay_sales
from app.admin_ebay_photos import router as admin_ebay_photos_router
from app.admin_inventory import router as admin_inventory_router
from app.admin_photos import ensure_columns as ensure_photo_columns
from app.admin_purchase_orders import ensure_po_types
from app.availability import ensure_tables as ensure_availability_tables
from app.availability import router as availability_router
from app.availability import start_background as start_availability
from app.admin_photos import router as admin_photos_router
from app.admin_purchase_orders import router as admin_purchase_orders_router
from app.admin_users import router as admin_users_router
from app.analytics import router as analytics_router
from app.analytics_qb import router as analytics_qb_router
from app.analytics_insights import router as analytics_insights_router
from app.autodist import router as autodist_router
from app.kits import router as kits_router
from app.order_activity import ensure_tables as ensure_order_activity_tables
from app.order_activity import router as order_activity_router
from app.warehouse_stock import ensure_tables as ensure_warehouse_tables
from app.warehouse_stock import router as warehouse_stock_router
from app.auth import AuthUser, require_staff
from app.config import get_settings
from app.database import get_db
from app.fitment import router as fitment_router
from app.fitment_research import router as fitment_research_router
from app.help_pages import admin_router as admin_help_router
from app.help_pages import store_router as store_help_router
from app.quickbooks import admin_router as admin_quickbooks_router
from app.quickbooks import soap_router as qbwc_router
from app.reviews import admin_router as admin_reviews_router
from app.reviews import store_router as store_reviews_router
from app.store_checkout import router as store_checkout_router
from app.store_extras import router as store_extras_router
from app.store_orders import router as store_orders_router

settings = get_settings()

app = FastAPI(title=settings.api_title, version=settings.api_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,  # bearer tokens, not cookies
)

admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_staff)])


@app.get("/health")
def health():
    return {"status": "ok"}


@admin_router.get("/me")
def me(user: AuthUser = Depends(require_staff)):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
        "permissions": sorted(user.permissions),
    }


@admin_router.get("/ping-db")
def ping_db(db: Session = Depends(get_db)):
    """Confirms the API can reach Postgres (use after deploying)."""
    db.execute(text("select 1"))
    return {"database": "ok"}


# Mount sub-routers on admin_router BEFORE admin_router itself is included,
# so they pick up the /admin prefix and the staff check.
admin_router.include_router(admin_catalog_router)
admin_router.include_router(admin_cleanup_router)
admin_router.include_router(admin_ebay_new_router)
admin_router.include_router(admin_ebay_listings_router)
admin_router.include_router(ebay_oauth_router)
admin_router.include_router(ebay_edit_router)
admin_router.include_router(ebay_copy_router)
admin_router.include_router(ebay_sales_router)
admin_router.include_router(ebay_ship_router)
admin_router.include_router(admin_ebay_photos_router)
admin_router.include_router(admin_ebay_compat_router)
admin_router.include_router(admin_ebay_fitment_router)
admin_router.include_router(admin_ebay_import_router)
admin_router.include_router(admin_quickbooks_router)
admin_router.include_router(admin_inventory_router)
admin_router.include_router(order_activity_router)
admin_router.include_router(warehouse_stock_router)
admin_router.include_router(admin_purchase_orders_router)
admin_router.include_router(admin_photos_router)
admin_router.include_router(availability_router)
admin_router.include_router(admin_users_router)
admin_router.include_router(admin_about_router)
admin_router.include_router(admin_reviews_router)
admin_router.include_router(admin_help_router)
admin_router.include_router(autodist_router)
admin_router.include_router(fitment_research_router)
admin_router.include_router(kits_router)
admin_router.include_router(analytics_router)
admin_router.include_router(analytics_qb_router)
admin_router.include_router(analytics_insights_router)

app.include_router(admin_router)
app.include_router(ebay_sales_public_router)  # optional outside pinger, protected by EBAY_POLL_SECRET
app.include_router(fitment_router)
app.include_router(store_extras_router)
app.include_router(store_checkout_router)
app.include_router(store_orders_router)
app.include_router(store_about_router)
app.include_router(store_reviews_router)
app.include_router(store_help_router)
app.include_router(qbwc_router)  # QuickBooks Web Connector (public URL, checks its own login)


@app.on_event("startup")
def _start_ebay_sales_timer():
    """Per-store eBay sales checks (the interval is set on the Live listings screen)."""
    start_ebay_sales()


@app.on_event("startup")
def _start_ebay_listing_timers():
    """Once-a-day eBay status check for the Listings screen, and the keeper that resets listings sitting at quantity 0."""
    start_status_sync_daily()
    start_zero_keeper()


@app.on_event("startup")
def _order_activity_tables():
    """The order activity log's tables (History tab > Order activity)."""
    ensure_order_activity_tables()
    ensure_warehouse_tables()


@app.on_event("startup")
def _photo_columns():
    """product_photos.hidden: lets a photo be turned off for the website and eBay without deleting it."""
    ensure_photo_columns()


@app.on_event("startup")
def _availability():
    """Distributor-aware availability: its columns/tables, and the timer that notices distributor stock changes (does nothing while its switch is OFF)."""
    ensure_availability_tables()
    start_availability()


@app.on_event("startup")
def _po_types():
    """Allow the 'stock_drop_ship' (inventory drop ship) purchase order type if the database restricts po_type."""
    ensure_po_types()
