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
