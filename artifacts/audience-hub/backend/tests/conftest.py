import os
import secrets
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost/audience_hub")
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("PII_HASH_PEPPER", secrets.token_hex(32))
os.environ["APP_ENV"] = "development"
os.environ["AUTH_MODE"] = "dev"