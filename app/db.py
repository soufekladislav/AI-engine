from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import get_settings

settings = get_settings()

if settings.database_url:
    db_url = settings.database_url
elif settings.db_name and settings.db_user:
    # URL.create safely handles special characters in passwords.
    db_url = URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query={"charset": "utf8mb4"},
    )
else:
    # Safe local fallback for first smoke tests. MariaDB is recommended for persistence.
    db_url = "sqlite:///./fresh_engine.db"

connect_args = {"check_same_thread": False} if str(db_url).startswith("sqlite") else {}
engine = create_engine(
    db_url,
    pool_pre_ping=True,
    pool_recycle=280,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
