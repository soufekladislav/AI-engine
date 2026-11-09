"""Small in-place schema upgrades for existing Fresh AI Engine databases.

SQLAlchemy's create_all() creates missing tables but does not add columns to an
existing table. Upgrades are deliberately tiny, repeatable and safe when more
than one Passenger worker starts at the same time.
"""
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


def ensure_schema_upgrades(engine):
    inspector = inspect(engine)
    if 'connectors' not in set(inspector.get_table_names()):
        return
    columns = {c['name'] for c in inspector.get_columns('connectors')}
    upgrades = []
    if 'expires_at' not in columns:
        upgrades.append(('expires_at', 'ALTER TABLE connectors ADD COLUMN expires_at DATETIME NULL'))
    if 'deleted_at' not in columns:
        upgrades.append(('deleted_at', 'ALTER TABLE connectors ADD COLUMN deleted_at DATETIME NULL'))
    for column, sql in upgrades:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except SQLAlchemyError:
            # Another Passenger worker may have added the same column after our
            # initial inspection. Only suppress the error when the column now exists.
            fresh = {c['name'] for c in inspect(engine).get_columns('connectors')}
            if column not in fresh:
                raise

    # v0.5.2: preserve nominal credit/tariff/FX snapshots for newly settled jobs.
    if 'job_billing' in set(inspect(engine).get_table_names()):
        billing_cols = {c['name'] for c in inspect(engine).get_columns('job_billing')}
        for name in ('credit_unit_price_czk','usd_czk_rate',
                     'input_price_usd_per_million','output_price_usd_per_million'):
            if name in billing_cols:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE job_billing ADD COLUMN {name} DOUBLE NULL'))
            except SQLAlchemyError:
                if name not in {c['name'] for c in inspect(engine).get_columns('job_billing')}:
                    raise

    # v0.5.4: store the concrete connector operation separately from the routing capability.
    if 'jobs' in set(inspect(engine).get_table_names()):
        job_cols = {c['name'] for c in inspect(engine).get_columns('jobs')}
        if 'operation' not in job_cols:
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE jobs ADD COLUMN operation VARCHAR(160) NULL'))
            except SQLAlchemyError:
                if 'operation' not in {c['name'] for c in inspect(engine).get_columns('jobs')}:
                    raise


    # v0.5.5: separate application data/output contract and persist validation metadata.
    if 'jobs' in set(inspect(engine).get_table_names()):
        job_cols = {c['name'] for c in inspect(engine).get_columns('jobs')}
        v055 = {
            'input_data_payload': 'ALTER TABLE jobs ADD COLUMN input_data_payload LONGTEXT NULL',
            'output_contract_payload': 'ALTER TABLE jobs ADD COLUMN output_contract_payload LONGTEXT NULL',
            'result_type': 'ALTER TABLE jobs ADD COLUMN result_type VARCHAR(20) NULL',
            'result_schema_id': 'ALTER TABLE jobs ADD COLUMN result_schema_id VARCHAR(160) NULL',
            'result_validated': 'ALTER TABLE jobs ADD COLUMN result_validated TINYINT(1) NOT NULL DEFAULT 0',
        }
        for name, sql in v055.items():
            if name in job_cols:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql))
            except SQLAlchemyError:
                if name not in {c['name'] for c in inspect(engine).get_columns('jobs')}:
                    raise


    # v0.5.6: optional editable Router 2.0 knowledge profile per model route.
    if 'model_routes' in set(inspect(engine).get_table_names()):
        model_cols = {c['name'] for c in inspect(engine).get_columns('model_routes')}
        if 'routing_profile_json' not in model_cols:
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE model_routes ADD COLUMN routing_profile_json LONGTEXT NULL'))
            except SQLAlchemyError:
                if 'routing_profile_json' not in {c['name'] for c in inspect(engine).get_columns('model_routes')}:
                    raise


    # v0.5.9: Router 2.0 traces and application payloads can legitimately exceed
    # MySQL TEXT (64 KiB).  Truncated JSON caused JSONDecodeError and HTTP 500.
    # Widen only on MySQL/MariaDB; other supported dialects already have an
    # effectively unbounded TEXT type.
    if engine.dialect.name in {'mysql', 'mariadb'}:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        def _needs_longtext(table, column):
            try:
                cols = {c['name']: c for c in inspect(engine).get_columns(table)}
                return 'LONGTEXT' not in str(cols[column]['type']).upper()
            except Exception:
                return True

        if 'routing_decisions' in tables and _needs_longtext('routing_decisions', 'trace_json'):
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE routing_decisions MODIFY trace_json LONGTEXT NOT NULL'))
            except SQLAlchemyError:
                if _needs_longtext('routing_decisions', 'trace_json'):
                    raise

        if 'jobs' in tables:
            desired = {
                'prompt_payload': 'LONGTEXT NULL',
                'context_payload': 'LONGTEXT NULL',
                'response_payload': 'LONGTEXT NULL',
            }
            for column, definition in desired.items():
                if not _needs_longtext('jobs', column):
                    continue
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f'ALTER TABLE jobs MODIFY {column} {definition}'))
                except SQLAlchemyError:
                    if _needs_longtext('jobs', column):
                        raise


# v0.5.7 adds no mandatory schema objects. Router history is fail-safe: if an
# optional history/feedback object is temporarily unavailable, selection falls
# back to static model knowledge instead of failing the request.
