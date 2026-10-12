import os

# Must be set before app modules import settings.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://testproj.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-test-secret-test-secret-32b")
