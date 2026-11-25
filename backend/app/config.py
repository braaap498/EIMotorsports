"""
Runtime configuration, read from environment variables (or a local .env).
Nothing here is specific to any marketplace: the website is the product.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_title: str = "E&I Motorsports API"
    api_version: str = "0.2.0"

    database_url: str

    supabase_url: str
    # Legacy HS256 projects only. Blank = verify via the project's JWKS.
    supabase_jwt_secret: str | None = None
    supabase_jwt_audience: str = "authenticated"
    # Secret key used ONLY by the server to add/edit staff accounts. Never put it in the browser.
    supabase_service_key: str | None = None

    # Fitment research (AI step). Blank = the AI button says it isn't set up.
    anthropic_api_key: str | None = None
    fitment_ai_model: str = "claude-sonnet-4-6"

    # Security switches for staff accounts (both off by default so nothing locks out before they are set up).
    # require_mfa: staff need a second factor (Supabase TOTP, token level aal2) to use the admin API.
    require_mfa: bool = False
    # password_max_age_days: staff whose password is older than this are refused until it is changed (0 = off; 365 = annual).
    password_max_age_days: int = 0

    allowed_origins: str = "http://localhost:3000"

    # Public base address of the Supabase Storage bucket that holds the photos.
    image_base_url: str = (
        "https://ezaqtxicodbcnnirwdfa.supabase.co/storage/v1/object/public/product-images"
    )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.jwt_issuer}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
