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

from app.auth import AuthUser, require_staff
from app.config import get_settings
from app.database import get_db
from app.fitment import router as fitment_router

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
    return {"user_id": user.user_id, "email": user.email, "role": user.role}


@admin_router.get("/ping-db")
def ping_db(db: Session = Depends(get_db)):
    """Confirms the API can reach Postgres (use after deploying)."""
    db.execute(text("select 1"))
    return {"database": "ok"}


app.include_router(admin_router)
app.include_router(fitment_router)
