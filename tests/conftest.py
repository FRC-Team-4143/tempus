"""
Shared pytest fixtures.

Every test runs against a fresh in-memory SQLite database. We use a StaticPool so
the single in-memory connection is shared across the session (in-memory DBs are
otherwise per-connection and would appear empty).
"""
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import FocusCategory, Student, Team


@pytest_asyncio.fixture
async def engine():
    """A fresh in-memory database engine with all tables created."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    """A database session for direct service-layer tests."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def team(db) -> Team:
    t = Team(number=4143, name="MARS/WARS")
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@pytest_asyncio.fixture
async def make_student(db, team):
    """Factory: create_student(name="Ada", code="abc123", category=..., active=True)."""
    async def _make(
        name: str = "Ada Lovelace",
        code: str = "ada00001",
        category: FocusCategory | None = FocusCategory.software,
        team_id: int | None = None,
        is_active: bool = True,
    ) -> Student:
        s = Student(
            name=name,
            student_code=code,
            team_id=team_id or team.id,
            category=category,
            is_active=is_active,
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s

    return _make


@pytest_asyncio.fixture
async def client(session_factory):
    """An httpx AsyncClient wired to the app with get_db overridden to the test DB."""
    import httpx
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def authed_client(client):
    """An httpx client already logged into the admin UI."""
    from app.config import settings

    resp = await client.post(
        "/admin/login", data={"password": settings.admin_password}
    )
    assert resp.status_code in (200, 303)
    return client
