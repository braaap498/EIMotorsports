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
from app.admin_photos import router as admin_photos_router
from app.admin_users import router as admin_users_router
from app.auth import AuthUser, require_staff
from app.config import get_settings
from app.database import get_db
from app.fitment import router as fitment_router
from app.store_checkout import router as store_checkout_router
from app.store_extras import router as store_extras_router

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
admin_router.include_router(admin_photos_router)
admin_router.include_router(admin_users_router)
admin_router.include_router(admin_about_router)

app.include_router(admin_router)
app.include_router(fitment_router)
app.include_router(store_extras_router)
app.include_router(store_checkout_router)
app.include_router(store_about_router)
