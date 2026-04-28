from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables and seed initial data."""
    from app.models import Team  # noqa: F401 – imported for side-effect (table creation)

    async with engine.begin() as conn:
        from app.database import Base
        await conn.run_sync(Base.metadata.create_all)

    await _seed_teams()


async def _seed_teams() -> None:
    from app.models import Team
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Team))
        existing = result.scalars().all()
        existing_numbers = {t.number for t in existing}

        for number, name in [(4143, "MARS/WARS"), (4423, "MARS' Minions")]:
            if number not in existing_numbers:
                session.add(Team(number=number, name=name))
            else:
                # Update name in case it was seeded with an old value
                team = next(t for t in existing if t.number == number)
                team.name = name

        await session.commit()
