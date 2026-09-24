from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file=".env")
    app_env: str = "development"
    database_url: str
    secret_key: str
    fernet_key: str
    pii_hash_pepper: str
    auth_mode: str = "oidc"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_role_claim: str = "groups"
    oidc_admin_groups: str = "ah-admins"
    oidc_analyst_groups: str = "ah-analysts"
    oidc_viewer_groups: str = "ah-viewers"
    public_base_url: str = ""
    upload_dir: str = "/data/uploads"
    export_dir: str = "/data/exports"
    default_phone_region: str = "US"
    gmail_style_domains: str = "gmail.com,googlemail.com"
    upload_retention_days: int = 7
    deletion_two_person: bool = True
    event_retention_days: int = 730
    export_retention_days: int = 14
    worker_concurrency: int = 4
    ingest_cors_origins: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_auth(self):
        if self.app_env == "production" and self.auth_mode == "dev":
            raise ValueError("AUTH_MODE=dev is forbidden in production")
        if self.auth_mode not in ("dev", "oidc"):
            raise ValueError("AUTH_MODE must be dev or oidc")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret, self.public_base_url)
        ):
            raise ValueError("OIDC requires issuer, client ID, client secret and public base URL")
        if self.app_env == "production" and len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()