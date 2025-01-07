import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Index, and_, create_engine, func, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Current time as naive UTC, the form every ``created_at`` is stored in."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CareNote(Base):
    __tablename__ = "care_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    facility_id: Mapped[int] = mapped_column(index=True)
    patient_id: Mapped[str]
    category: Mapped[str]  # 'medication', 'observation', 'treatment'
    priority: Mapped[int]  # 1-5
    created_at: Mapped[datetime] = mapped_column(index=True)
    created_by: Mapped[str]
    note_content: Mapped[str]

    __table_args__ = (
        Index('idx_tenant_facility_date', 'tenant_id', 'facility_id', 'created_at'),
        Index('idx_tenant_date_category', 'tenant_id', 'created_at', 'category'),
    )


class CareNoteCreate(BaseModel):
    """Fields a client may set; ``id`` is always assigned by the database."""

    model_config = ConfigDict(extra='forbid')

    tenant_id: int = Field(default=1, ge=1)
    facility_id: int = Field(default=1, ge=1)
    patient_id: str = Field(min_length=1, max_length=64)
    category: Literal['medication', 'observation', 'treatment']
    priority: int = Field(ge=1, le=5)
    created_by: str = Field(min_length=1, max_length=64)
    note_content: str = Field(min_length=1, max_length=10_000)
    created_at: datetime | None = None

    @field_validator('created_at')
    @classmethod
    def store_as_naive_utc(cls, value: datetime | None) -> datetime | None:
        # Notes are stored as naive UTC; an offset would otherwise be silently dropped.
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value


class CareNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    facility_id: int
    patient_id: str
    category: str
    priority: int
    created_at: datetime
    created_by: str
    note_content: str


class Pagination(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int


class CareNotePage(BaseModel):
    notes: list[CareNoteRead]
    pagination: Pagination


DATABASE_FILE = os.getenv("CARE_NOTES_DB_PATH", "carenotes.db")
DATABASE_URL = URL.create("sqlite", database=DATABASE_FILE)
ASYNC_DATABASE_URL = URL.create("sqlite+aiosqlite", database=DATABASE_FILE)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CARE_NOTES_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

# Schema creation stays synchronous so the seeder and startup share one simple path.
sync_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
async_engine = create_async_engine(ASYNC_DATABASE_URL, connect_args={"check_same_thread": False})
async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)


def init_db():
    """Create missing tables; existing notes are never dropped."""
    Base.metadata.create_all(bind=sync_engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Care-note records API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_db():
    async with async_session_maker() as session:
        yield session


DEMO_CATEGORIES = ('medication', 'observation', 'treatment')
SEED_BATCH_SIZE = 1000


async def create_test_data(num_notes: int = 1000, rng: random.Random | None = None):
    """Append ``num_notes`` fictional notes spread over the last 30 days.

    All batches go through one session and one commit: a failed run leaves no
    partial data, and SQLite syncs to disk once instead of once per batch.
    """
    rng = rng or random.Random()
    now = utc_now()
    tenants = range(1, 11)
    facilities_per_tenant = 5
    patients_per_facility = 50

    logger.info("Generating %d synthetic care notes", num_notes)
    async with async_session_maker() as session:
        for batch_start in range(0, num_notes, SEED_BATCH_SIZE):
            batch = []
            for _ in range(min(SEED_BATCH_SIZE, num_notes - batch_start)):
                tenant_id = rng.choice(tenants)
                facility_id = tenant_id * 10 + rng.randint(1, facilities_per_tenant)
                patient_id = f"DEMO-P{facility_id}-{rng.randint(1, patients_per_facility):02d}"
                # Weighted towards recent days, like a live care-home log.
                age = timedelta(days=rng.triangular(0, 30, 5))
                batch.append(
                    {
                        'tenant_id': tenant_id,
                        'facility_id': facility_id,
                        'patient_id': patient_id,
                        'category': rng.choice(DEMO_CATEGORIES),
                        'priority': rng.randint(1, 5),
                        'created_at': now - age,
                        'created_by': f"demo-carer-{rng.randint(1, 20):02d}",
                        'note_content': f"Synthetic demo note for fictional resident {patient_id}",
                    }
                )
            await session.execute(insert(CareNote), batch)
        await session.commit()
    logger.info("Inserted %d synthetic care notes", num_notes)


async def get_daily_care_stats(
    db: AsyncSession, tenant_id: int, facility_ids: list[int] | None = None, day: datetime | None = None
):
    """Benchmark baseline: load the day's notes and count them in Python.

    ``func.date()`` on the column also keeps SQLite from using the date index.
    """
    if day is None:
        day = utc_now()

    base_query = select(CareNote).where(
        CareNote.tenant_id == tenant_id, func.date(CareNote.created_at) == day.date().isoformat()
    )
    if facility_ids:
        base_query = base_query.where(CareNote.facility_id.in_(facility_ids))

    notes = (await db.execute(base_query)).scalars().all()

    stats = {
        "total_notes": len(notes),
        "by_category": {},
        "by_priority": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        "by_facility": {},
    }
    for note in notes:
        stats["by_category"][note.category] = stats["by_category"].get(note.category, 0) + 1
        stats["by_priority"][note.priority] = stats["by_priority"].get(note.priority, 0) + 1
        stats["by_facility"][note.facility_id] = stats["by_facility"].get(note.facility_id, 0) + 1
    return stats


async def get_daily_care_stats_optimized(
    db: AsyncSession,
    tenant_id: int,
    facility_ids: list[int] | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate counts in SQL rather than loading every matching note.

    A missing bound leaves that side of the range open (used by ``all_time``).
    """
    filters = [CareNote.tenant_id == tenant_id]
    if start_date is not None:
        filters.append(CareNote.created_at >= start_date)
    if end_date is not None:
        filters.append(CareNote.created_at <= end_date)
    if facility_ids:
        filters.append(CareNote.facility_id.in_(facility_ids))

    # One query per grouping: SQLite has no GROUPING SETS.
    total_query = select(
        func.count(CareNote.id).label('total_notes'),
        func.count(func.distinct(CareNote.patient_id)).label('unique_patients'),
    ).where(and_(*filters))
    total_result = (await db.execute(total_query)).first()

    category_query = (
        select(CareNote.category, func.count(CareNote.id).label('count'))
        .where(and_(*filters))
        .group_by(CareNote.category)
    )
    category_results = (await db.execute(category_query)).all()

    priority_query = (
        select(CareNote.priority, func.count(CareNote.id).label('count'))
        .where(and_(*filters))
        .group_by(CareNote.priority)
    )
    priority_results = (await db.execute(priority_query)).all()

    facility_query = (
        select(CareNote.facility_id, func.count(CareNote.id).label('count'))
        .where(and_(*filters))
        .group_by(CareNote.facility_id)
    )
    facility_results = (await db.execute(facility_query)).all()

    stats = {
        "total_notes": total_result.total_notes or 0,
        "by_category": {row.category: row.count for row in category_results},
        "by_priority": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        "by_facility": {row.facility_id: row.count for row in facility_results},
        "avg_notes_per_patient": 0,
        "date_range": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
    }

    for row in priority_results:
        stats["by_priority"][row.priority] = row.count

    if total_result.unique_patients and total_result.unique_patients > 0:
        stats["avg_notes_per_patient"] = round(stats["total_notes"] / total_result.unique_patients, 2)

    return stats


COMPARED_STAT_KEYS = ("total_notes", "by_category", "by_priority", "by_facility")


async def run_performance_test(num_iterations: int = 10, concurrent_requests: int = 5) -> dict[str, Any]:
    """Time the in-memory baseline against SQL aggregation over the same calendar day."""
    tenant_id = 1
    facility_ids = [11, 12, 13, 14, 15]
    # The seeder weights notes towards five days ago, so that day has data.
    day = utc_now() - timedelta(days=5)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day.replace(hour=23, minute=59, second=59, microsecond=999999)

    async def timed(compute):
        async with async_session_maker() as db:
            started = time.perf_counter()
            result = await compute(db)
            return time.perf_counter() - started, result

    async def in_memory(db):
        return await get_daily_care_stats(db, tenant_id, facility_ids, day)

    async def sql_aggregate(db):
        return await get_daily_care_stats_optimized(db, tenant_id, facility_ids, day_start, day_end)

    async def measure(compute):
        sequential = [await timed(compute) for _ in range(num_iterations)]
        started = time.perf_counter()
        await asyncio.gather(*(timed(compute) for _ in range(concurrent_requests)))
        concurrent_total = time.perf_counter() - started
        average = sum(elapsed for elapsed, _ in sequential) / num_iterations
        return average, concurrent_total, sequential[0][1]

    baseline_avg, baseline_concurrent, baseline_stats = await measure(in_memory)
    sql_avg, sql_concurrent, sql_stats = await measure(sql_aggregate)

    return {
        "tenant_id": tenant_id,
        "facility_ids": facility_ids,
        "day": day_start.date().isoformat(),
        "notes_in_window": sql_stats["total_notes"],
        "results_match": all(baseline_stats[key] == sql_stats[key] for key in COMPARED_STAT_KEYS),
        "sequential": {
            "iterations": num_iterations,
            "in_memory_avg_ms": round(baseline_avg * 1000, 3),
            "sql_aggregate_avg_ms": round(sql_avg * 1000, 3),
        },
        "concurrent": {
            "requests": concurrent_requests,
            "in_memory_total_ms": round(baseline_concurrent * 1000, 3),
            "sql_aggregate_total_ms": round(sql_concurrent * 1000, 3),
        },
    }


def parse_facility_ids(raw: str | None) -> list[int] | None:
    """Parse a comma-separated facility filter; an absent or empty value means no filter."""
    if not raw:
        return None
    try:
        facility_ids = [int(value.strip()) for value in raw.split(',') if value.strip()]
        if not facility_ids or any(value < 1 for value in facility_ids):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Facility IDs must be positive integers") from None
    return facility_ids


def parse_range_bound(value: str, end_of_day: bool) -> datetime:
    """Parse an ISO date or datetime into the naive-UTC form the notes are stored in.

    A bare date as the end bound covers that whole day, so
    ``start_date=2026-11-01&end_date=2026-11-01`` returns the notes of 1 November.
    """
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            if end_of_day:
                return datetime.combine(day, datetime.max.time())
            return datetime.combine(day, datetime.min.time())
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def resolve_date_range(
    range_name: str, start_date: str | None, end_date: str | None, now: datetime
) -> tuple[datetime | None, datetime | None]:
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=400, detail="start_date and end_date must be supplied together")
    if start_date is not None:
        start = parse_range_bound(start_date, end_of_day=False)
        end = parse_range_bound(end_date, end_of_day=True)
        if start > end:
            raise HTTPException(status_code=400, detail="start_date must not be after end_date")
        return start, end

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    if range_name == "today":
        return midnight, end_of_today
    if range_name == "this_week":
        return midnight - timedelta(days=now.weekday()), end_of_today
    if range_name == "this_month":
        return midnight.replace(day=1), end_of_today
    if range_name == "this_year":
        return midnight.replace(month=1, day=1), end_of_today
    if range_name == "all_time":
        return None, None
    raise HTTPException(status_code=400, detail="Invalid date range")


@app.get("/api/care-stats")
async def get_care_stats(
    tenant_id: int,
    facility_ids: str | None = None,
    range_name: str = Query(default="today", alias="range"),
    start_date: str | None = None,
    end_date: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get care statistics for a tenant within a date range."""
    facility_id_list = parse_facility_ids(facility_ids)
    start, end = resolve_date_range(range_name, start_date, end_date, utc_now())
    return await get_daily_care_stats_optimized(db, tenant_id, facility_id_list, start, end)


@app.post("/api/run-performance-test")
async def run_perf_test(
    iterations: int = Query(default=10, ge=1, le=50),
    concurrency: int = Query(default=5, ge=1, le=20),
):
    """Compare the in-memory baseline with SQL aggregation and return the timings."""
    return await run_performance_test(iterations, concurrency)


@app.get("/api/care-notes", response_model=CareNotePage)
async def get_care_notes(
    tenant_id: int | None = None,
    facility_ids: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get care notes with pagination and optional filtering."""
    offset = (page - 1) * page_size

    # id breaks timestamp ties so a note never shows up on two pages.
    query = select(CareNote).order_by(CareNote.created_at.desc(), CareNote.id.desc())

    if tenant_id is not None:
        query = query.where(CareNote.tenant_id == tenant_id)

    facility_id_list = parse_facility_ids(facility_ids)
    if facility_id_list:
        query = query.where(CareNote.facility_id.in_(facility_id_list))

    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    notes = result.scalars().all()

    count_query = select(func.count()).select_from(CareNote)
    if tenant_id is not None:
        count_query = count_query.where(CareNote.tenant_id == tenant_id)
    if facility_id_list:
        count_query = count_query.where(CareNote.facility_id.in_(facility_id_list))

    total_count = await db.scalar(count_query)
    total_pages = (total_count + page_size - 1) // page_size

    return {
        "notes": notes,
        "pagination": {
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        },
    }


@app.post("/api/care-notes", response_model=CareNoteRead, status_code=201)
async def create_care_note(note: CareNoteCreate, db: AsyncSession = Depends(get_db)):
    """Create a care note; ``created_at`` defaults to the current UTC time."""
    fields = note.model_dump()
    if fields['created_at'] is None:
        fields['created_at'] = utc_now()

    db_note = CareNote(**fields)
    db.add(db_note)
    await db.commit()
    await db.refresh(db_note)
    return db_note


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
