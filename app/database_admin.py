"""Manual migration of an existing Engine DB to a freshly provisioned MariaDB.

DDL is intentionally separate from the row-copy transaction: a failed copy can
be retried without deleting the already created target tables. Existing target
primary keys are never overwritten.
"""
from __future__ import annotations
import os
import shutil
from pathlib import Path

from sqlalchemy import URL, and_, create_engine, literal, select
from app.db import Base, engine as current_engine
from app.config import ENV_FILE


def target_url(host, port, name, user, password):
    return URL.create('mysql+pymysql', username=user, password=password,
                      host=host, port=int(port), database=name,
                      query={'charset': 'utf8mb4'})


def test_connection(host, port, name, user, password):
    target = create_engine(target_url(host, port, name, user, password), pool_pre_ping=True)
    try:
        with target.connect() as connection:
            connection.exec_driver_sql('SELECT 1')
    finally:
        target.dispose()
    return True


def _copy_rows(source, destination, tables):
    """Copy missing PKs using SQLAlchemy expressions and driver-safe parameters."""
    counts = {}
    for table in tables:
        primary_key = list(table.primary_key.columns)
        if not primary_key:
            raise ValueError(f'Tabulka {table.name} nemá primární klíč; import byl zastaven.')
        copied = 0
        # partitions limits memory consumption for larger histories.
        result = source.execution_options(stream_results=True).execute(select(table))
        for partition in result.mappings().partitions(250):
            for row in partition:
                record = dict(row)
                matches = and_(*(col == record[col.name] for col in primary_key))
                # Do not use exec_driver_sql here: it expects native DB-API
                # placeholders, not SQLAlchemy's :name bind expressions.
                exists = destination.execute(
                    select(literal(1)).select_from(table).where(matches).limit(1)
                ).first() is not None
                if not exists:
                    destination.execute(table.insert().values(**record))
                    copied += 1
        counts[table.name] = copied
    return counts


def migrate_current(host, port, name, user, password):
    target = create_engine(target_url(host, port, name, user, password), pool_pre_ping=True)
    try:
        # A prior failed run may have created tables: create_all is safe to rerun.
        Base.metadata.create_all(target)
        # Existing customer data must remain intact; check that both DBs expose
        # the same expected schema before attempting inserts into the target.
        from sqlalchemy import inspect
        target_inspector = inspect(target)
        source_inspector = inspect(current_engine)
        for table in Base.metadata.sorted_tables:
            if not source_inspector.has_table(table.name):
                raise ValueError(f'Zdrojová databáze nemá tabulku {table.name}; import se nespustil.')
            source_columns = {c['name'] for c in source_inspector.get_columns(table.name)}
            target_columns = {c['name'] for c in target_inspector.get_columns(table.name)}
            expected = set(table.columns.keys())
            if not expected <= source_columns or not expected <= target_columns:
                raise ValueError(f'Tabulka {table.name} má neúplnou strukturu. '
                                 'Nejdříve oprav schéma; nedokončený import lze poté opakovat.')
        # Inserts are transactional on InnoDB. An exception rolls back this
        # attempt; target tables from create_all may legitimately remain.
        with current_engine.connect() as source, target.begin() as destination:
            return _copy_rows(source, destination, Base.metadata.sorted_tables)
    finally:
        target.dispose()


def save_env_database(host, port, name, user, password):
    path = Path(ENV_FILE)
    content = path.read_text(encoding='utf-8') if path.exists() else ''
    backup = path.with_name(path.name + '.bak')
    if path.exists():
        shutil.copy2(path, backup)
    def quoted(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'
    values = {'DATABASE_URL': '', 'DB_HOST': quoted(host), 'DB_PORT': str(port),
              'DB_NAME': quoted(name), 'DB_USER': quoted(user),
              'DB_PASSWORD': quoted(password)}
    lines = content.splitlines()
    found = set()
    output = []
    for line in lines:
        if '=' in line and not line.lstrip().startswith('#'):
            key = line.split('=', 1)[0].strip()
            if key in values:
                output.append(f'{key}={values[key]}')
                found.add(key)
                continue
        output.append(line)
    for key, value in values.items():
        if key not in found:
            output.append(f'{key}={value}')
    tmp = path.with_suffix('.tmp')
    tmp.write_text('\n'.join(output).rstrip() + '\n', encoding='utf-8')
    os.replace(tmp, path)
    return str(backup) if backup.exists() else ''
