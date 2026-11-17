import threading

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# pool_pre_ping: antes de usar una conexión del pool verifica que siga viva.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependencia de FastAPI: una sesión de base de datos por request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SqlQueryCounter:
    """
    Cuenta cuántas sentencias SQL ejecuta ESTE proceso de la API.
    Se usa en la Fase 2 para demostrar cuántas consultas a PostgreSQL
    se ahorra el caché de Redis (ver GET /metrics).
    """

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    def reset(self) -> None:
        with self._lock:
            self._value = 0

    @property
    def value(self) -> int:
        return self._value


sql_query_counter = SqlQueryCounter()


@event.listens_for(Engine, "before_cursor_execute")
def _count_sql_queries(conn, cursor, statement, parameters, context, executemany):
    sql_query_counter.increment()
