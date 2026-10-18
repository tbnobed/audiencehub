"""Reference rows are not profiles or vendor enrichment."""
from datetime import datetime
from sqlalchemy import DateTime, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ResourceColumns:
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    series: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceCode(ResourceColumns, Base):
    __tablename__ = "source_codes"


class Product(ResourceColumns, Base):
    __tablename__ = "products"


class VodTitle(ResourceColumns, Base):
    __tablename__ = "vod_titles"
