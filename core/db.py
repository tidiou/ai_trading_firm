"""
Database session management.

Design notes:

- session_scope() is a context manager, not a raw session getter.
  It commits on success and rolls back on ANY exception, then always
  closes the connection. This is the guarantee that a bug halfway
  through, say, Vera writing her daily monitoring rows can never
  leave the database in a half-written state — commit-or-rollback,
  never partial. Given Otis's ledger depends on data integrity,
  this is worth treating as non-negotiable, not a nicety.

- Connection pooling is deliberately modest (pool_size=5). This is a
  once-daily batch job, not a high-concurrency web app — we don't
  need (or want) a large connection pool sitting open against Railway
  Postgres between runs.

- load_dotenv() is called here defensively so any script that imports
  this module (an agent module in isolation, a one-off test, a REPL
  session) gets DATABASE_URL without needing to remember to load
  .env first at some higher entry point.
"""

import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=2,
    pool_pre_ping=True,  # avoids using a stale connection Railway has silently closed
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope():
    """
    Usage:
        with session_scope() as session:
            session.add(some_row)
        # auto-committed here, or rolled back if an exception was raised
    """
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection() -> bool:
    """Quick sanity check — run this first after setting up .env."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


if __name__ == "__main__":
    # python core/db.py  ->  quick manual connectivity check
    if check_connection():
        print("Database connection OK.")
