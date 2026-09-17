from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from alembic import context

from app.database import Base
from app.models import Job, Application, Approval, CandidateAnswer, CandidateProfile, Event, SourceRun, SubmissionNonce  # noqa: F401
from sqlalchemy import inspect as sa_inspect
from app.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url():
    url = config.get_main_option("sqlalchemy.url")
    if not url or url == "sqlite:///./jobhunter.db":
        url = settings.database_url
    return url


def run_migrations_offline():
    url = _get_url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    url = _get_url()
    connect_args = {"check_same_thread": False} if "sqlite" in url else {}
    connectable = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with connectable.connect() as connection:
        inspector = sa_inspect(connection)
        if "jobs" not in inspector.get_table_names():
            target_metadata.create_all(bind=connection)
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
