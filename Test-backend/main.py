from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy import create_engine, func, select, Index, and_, text
from sqlalchemy.engine import URL
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, DeclarativeBase, Mapped, mapped_column, sessionmaker
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict, Any
import asyncio
import random
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware

# Base Models
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

# Database setup
DATABASE_FILE = os.getenv("CARE_NOTES_DB_PATH", "carenotes.db")
DATABASE_URL = URL.create("sqlite", database=DATABASE_FILE)
ASYNC_DATABASE_URL = URL.create("sqlite+aiosqlite", database=DATABASE_FILE)

# Sync engine for simpler setup
sync_engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})

# Async engine for async operations
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False}
)
async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Frontend URL
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Dependency to get DB session
async def get_db():
    async with async_session_maker() as session:
        yield session

# Initialize database
def init_db():
    Base.metadata.create_all(bind=sync_engine)

# Async function to populate test data
async def create_test_data(num_notes: int = 100000):
    """Populate database with test care notes using efficient batch insertion."""
    categories = ['medication', 'observation', 'treatment']
    tenants = list(range(1, 11))  # 10 tenants
    facilities_per_tenant = 5
    patients_per_facility = 50

    batch_size = 1000
    notes_data = []

    # Generate timestamps for the last 30 days
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)

    print(f"Generating {num_notes} care notes...")

    for i in range(num_notes):
        tenant_id = random.choice(tenants)
        facility_id = tenant_id * 10 + random.randint(1, facilities_per_tenant)
        patient_id = f"P{facility_id}_{random.randint(1, patients_per_facility)}"

        # Realistic timestamp distribution (more recent notes)
        days_ago = random.triangular(0, 30, 5)
        timestamp = end_date - timedelta(days=days_ago,
                                       hours=random.randint(0, 23),
                                       minutes=random.randint(0, 59))

        note = {
            'tenant_id': tenant_id,
            'facility_id': facility_id,
            'patient_id': patient_id,
            'category': random.choice(categories),
            'priority': random.randint(1, 5),
            'created_at': timestamp,
            'created_by': f"nurse_{random.randint(1, 20)}",
            'note_content': f"Test care note content for patient {patient_id}"
        }
        notes_data.append(note)

        # Batch insert
        if len(notes_data) >= batch_size:
            async with async_session_maker() as session:
                await session.execute(
                    text("""
                    INSERT INTO care_notes (tenant_id, facility_id, patient_id, category, priority, created_at, created_by, note_content)
                    VALUES (:tenant_id, :facility_id, :patient_id, :category, :priority, :created_at, :created_by, :note_content)
                    """),
                    notes_data
                )
                await session.commit()
            notes_data = []
            if (i + 1) % 10000 == 0:
                print(f"Inserted {i + 1} notes...")

    # Insert remaining notes
    if notes_data:
        async with async_session_maker() as session:
            await session.execute(
                text("""
                INSERT INTO care_notes (tenant_id, facility_id, patient_id, category, priority, created_at, created_by, note_content)
                VALUES (:tenant_id, :facility_id, :patient_id, :category, :priority, :created_at, :created_by, :note_content)
                """),
                notes_data
            )
            await session.commit()

    print(f"Successfully inserted {num_notes} care notes!")

# Original inefficient implementation
async def get_daily_care_stats(
    db: AsyncSession,
    tenant_id: int,
    facility_ids: Optional[List[int]] = None,
    date: Optional[datetime] = None
):
    if date is None:
        date = datetime.utcnow()

    # Current inefficient query
    base_query = select(CareNote).where(
        CareNote.tenant_id == tenant_id,
        func.date(CareNote.created_at) == date.date()
    )

    if facility_ids:
        base_query = base_query.where(CareNote.facility_id.in_(facility_ids))

    notes = (await db.execute(base_query)).scalars().all()

    # Inefficient in-memory processing
    stats = {
        "total_notes": len(notes),
        "by_category": {},
        "by_priority": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        "by_facility": {},
        "avg_notes_per_patient": 0
    }

    for note in notes:
        stats["by_category"][note.category] = stats["by_category"].get(note.category, 0) + 1
        stats["by_priority"][note.priority] += 1
        stats["by_facility"][note.facility_id] = stats["by_facility"].get(note.facility_id, 0) + 1

    return stats

# Optimized implementation using SQL aggregations
async def get_daily_care_stats_optimized(
    db: AsyncSession,
    tenant_id: int,
    facility_ids: Optional[List[int]] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """Aggregate counts in SQL rather than loading every matching note."""
    if start_date is None:
        start_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if end_date is None:
        end_date = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)

    print(f"Calculating stats for tenant {tenant_id}, facilities {facility_ids}, date range: {start_date} to {end_date}")

    # Build base filter conditions
    filters = [
        CareNote.tenant_id == tenant_id,
        CareNote.created_at >= start_date,
        CareNote.created_at <= end_date
    ]

    if facility_ids:
        filters.append(CareNote.facility_id.in_(facility_ids))

    print(f"Using filters: {filters}")

    # Multiple optimized queries (SQLite doesn't support GROUPING SETS)
    # Get total count and unique patients
    total_query = (
        select(
            func.count(CareNote.id).label('total_notes'),
            func.count(func.distinct(CareNote.patient_id)).label('unique_patients')
        )
        .where(and_(*filters))
    )
    total_result = (await db.execute(total_query)).first()
    print(f"Total query result: {total_result}")

    # Get counts by category
    category_query = (
        select(
            CareNote.category,
            func.count(CareNote.id).label('count')
        )
        .where(and_(*filters))
        .group_by(CareNote.category)
    )
    category_results = (await db.execute(category_query)).all()
    print(f"Category results: {category_results}")

    # Get counts by priority
    priority_query = (
        select(
            CareNote.priority,
            func.count(CareNote.id).label('count')
        )
        .where(and_(*filters))
        .group_by(CareNote.priority)
    )
    priority_results = (await db.execute(priority_query)).all()
    print(f"Priority results: {priority_results}")

    # Get counts by facility
    facility_query = (
        select(
            CareNote.facility_id,
            func.count(CareNote.id).label('count')
        )
        .where(and_(*filters))
        .group_by(CareNote.facility_id)
    )
    facility_results = (await db.execute(facility_query)).all()
    print(f"Facility results: {facility_results}")

    # Build stats
    stats = {
        "total_notes": total_result.total_notes or 0,
        "by_category": {row.category: row.count for row in category_results},
        "by_priority": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        "by_facility": {row.facility_id: row.count for row in facility_results},
        "avg_notes_per_patient": 0,
        "date_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat()
        }
    }

    # Update priority counts
    for row in priority_results:
        stats["by_priority"][row.priority] = row.count

    # Calculate average notes per patient
    if total_result.unique_patients and total_result.unique_patients > 0:
        stats["avg_notes_per_patient"] = round(stats["total_notes"] / total_result.unique_patients, 2)

    print(f"Returning stats: {stats}")
    return stats

# Performance testing function
async def run_performance_test(num_iterations: int = 10, concurrent_requests: int = 5):
    """Compare performance of original vs optimized query."""
    print("\n=== Performance Test Results ===\n")

    # Test parameters
    test_tenant_id = 1
    test_facility_ids = [11, 12, 13, 14, 15]  # Facilities for tenant 1
    # Use a date 5 days ago to ensure we have data
    test_date = datetime.utcnow() - timedelta(days=5)

    # Test original implementation
    print("Testing ORIGINAL implementation...")
    original_times = []

    async def run_original():
        async with async_session_maker() as db:
            start = time.time()
            await get_daily_care_stats(db, test_tenant_id, test_facility_ids, test_date)
            end = time.time()
            return end - start

    # Sequential test for original
    for i in range(num_iterations):
        exec_time = await run_original()
        original_times.append(exec_time)

    # Concurrent test for original
    concurrent_original_start = time.time()
    tasks = [run_original() for _ in range(concurrent_requests)]
    await asyncio.gather(*tasks)
    concurrent_original_time = time.time() - concurrent_original_start

    # Test optimized implementation
    print("\nTesting OPTIMIZED implementation...")
    optimized_times = []

    async def run_optimized():
        async with async_session_maker() as db:
            start = time.time()
            await get_daily_care_stats_optimized(db, test_tenant_id, test_facility_ids, test_date, test_date)
            end = time.time()
            return end - start

    # Sequential test for optimized
    for i in range(num_iterations):
        exec_time = await run_optimized()
        optimized_times.append(exec_time)

    # Concurrent test for optimized
    concurrent_optimized_start = time.time()
    tasks = [run_optimized() for _ in range(concurrent_requests)]
    await asyncio.gather(*tasks)
    concurrent_optimized_time = time.time() - concurrent_optimized_start

    # Calculate metrics
    avg_original = sum(original_times) / len(original_times)
    avg_optimized = sum(optimized_times) / len(optimized_times)
    improvement = ((avg_original - avg_optimized) / avg_original) * 100

    # Get sample stats to show record count
    async with async_session_maker() as db:
        sample_stats = await get_daily_care_stats(db, test_tenant_id, test_facility_ids, test_date)
        print(f"\nProcessing {sample_stats['total_notes']} records for date: {test_date.date()}")

    # Print results
    print("\n--- Sequential Performance ---")
    print(f"Original avg time: {avg_original:.4f}s")
    print(f"Optimized avg time: {avg_optimized:.4f}s")
    print(f"Performance improvement: {improvement:.1f}%")
    print(f"Speed up: {avg_original/avg_optimized:.2f}x faster")

    print("\n--- Concurrent Performance ---")
    print(f"Original ({concurrent_requests} concurrent): {concurrent_original_time:.4f}s")
    print(f"Optimized ({concurrent_requests} concurrent): {concurrent_optimized_time:.4f}s")
    print(f"Concurrent improvement: {((concurrent_original_time - concurrent_optimized_time) / concurrent_original_time * 100):.1f}%")

    print("\n--- Resource Efficiency ---")
    print(f"Original: Loads all records into memory")
    print(f"Optimized: SQL aggregation (minimal memory usage)")

# API Endpoints
@app.on_event("startup")
async def startup_event():
    init_db()

@app.get("/api/care-stats")
async def get_care_stats(
    tenant_id: int,
    facility_ids: Optional[str] = None,
    range: str = "today",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    optimized: bool = True,
    db: AsyncSession = Depends(get_db)
):
    """Get care statistics for a tenant within a date range."""
    print(f"Received stats request - tenant_id: {tenant_id}, facility_ids: {facility_ids}, range: {range}")

    # Parse facility IDs
    facility_id_list = None
    if facility_ids:
        try:
            facility_id_list = [int(x.strip()) for x in facility_ids.split(",") if x.strip()]
            print(f"Parsed facility IDs: {facility_id_list}")
        except ValueError as e:
            print(f"Error parsing facility IDs: {e}")
            raise HTTPException(status_code=400, detail="Invalid facility IDs format")

    # Calculate date range
    now = datetime.utcnow()
    start = None
    end = None

    if start_date and end_date:
        # Use custom date range if provided
        try:
            start = datetime.fromisoformat(start_date)
            end = datetime.fromisoformat(end_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid date format")
    else:
        # Use predefined ranges
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        if range == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif range == "this_week":
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        elif range == "this_month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif range == "this_year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif range == "all_time":
            start = datetime.min
            end = datetime.max
        else:
            raise HTTPException(status_code=400, detail="Invalid date range")

    try:
        stats = await get_daily_care_stats_optimized(db, tenant_id, facility_id_list, start, end)
        print(f"Returning stats response: {stats}")
        return stats
    except Exception as e:
        print(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/run-performance-test")
async def run_perf_test():
    """Run performance comparison test."""
    await run_performance_test()
    return {"message": "Performance test completed. Check console for results."}

@app.get("/api/care-notes")
async def get_care_notes(
    tenant_id: Optional[int] = None,
    facility_ids: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    """Get care notes with pagination and optional filtering."""
    # Calculate offset
    offset = (page - 1) * page_size

    # Build base query
    query = select(CareNote).order_by(CareNote.created_at.desc())

    # Add filters
    if tenant_id is not None:
        query = query.where(CareNote.tenant_id == tenant_id)

    if facility_ids:
        try:
            facility_id_list = [int(value.strip()) for value in facility_ids.split(',') if value.strip()]
            if not facility_id_list or any(value < 1 for value in facility_id_list):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Facility IDs must be positive integers")
        query = query.where(CareNote.facility_id.in_(facility_id_list))

    # Add pagination
    query = query.offset(offset).limit(page_size)

    # Execute query
    result = await db.execute(query)
    notes = result.scalars().all()

    # Get total count for pagination
    count_query = select(func.count()).select_from(CareNote)
    if tenant_id is not None:
        count_query = count_query.where(CareNote.tenant_id == tenant_id)
    if facility_ids:
        count_query = count_query.where(CareNote.facility_id.in_(facility_id_list))

    total_count = await db.scalar(count_query)
    total_pages = (total_count + page_size - 1) // page_size

    return {
        "notes": notes,
        "pagination": {
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages
        }
    }

@app.post("/api/care-notes")
async def create_care_note(
    note: dict,
    db: AsyncSession = Depends(get_db)
):
    """Create a new care note."""
    # Set defaults if not provided
    note.setdefault('tenant_id', 1)
    note.setdefault('facility_id', 1)
    note.setdefault('created_at', datetime.utcnow())

    db_note = CareNote(**note)
    db.add(db_note)
    await db.commit()
    await db.refresh(db_note)
    return db_note

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)