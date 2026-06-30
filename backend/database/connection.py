"""
Pool de connexions PostgreSQL pour Supabase.
Max 10 connexions simultanées (DB_POOL_SIZE + DB_MAX_OVERFLOW).
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", 5))
DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", 5))

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_pre_ping=True,        # vérifie la connexion avant usage
    pool_recycle=1800,         # recycle les connexions après 30 min
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """
    Générateur de session SQLAlchemy pour l'injection de dépendance FastAPI.
    Ferme la session après usage dans tous les cas.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_connection() -> bool:
    """
    Teste la connexion à la base de données.
    Retourne True si la connexion réussit, False sinon.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"[connection] Échec de connexion à la base : {e}")
        return False
