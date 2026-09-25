"""Persisted public dashboard aggregates (additional internal facts use SQL DDL)."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Identity, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class DashboardDaily(Base):
    __tablename__ = "dashboard_daily"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    source_id: Mapped[int] = mapped_column(BigInteger)
    fund: Mapped[str | None] = mapped_column(Text)
    campaign: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    gift_count: Mapped[int] = mapped_column(BigInteger)
    gift_amount: Mapped[Decimal] = mapped_column(Numeric)
    donor_count: Mapped[int] = mapped_column(BigInteger)
    new_donor_count: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean)


class DashboardKPI(Base):
    __tablename__ = "dashboard_kpis"
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)


class DashboardCache(Base):
    __tablename__ = "dashboard_cache"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generation: Mapped[str] = mapped_column(Text)