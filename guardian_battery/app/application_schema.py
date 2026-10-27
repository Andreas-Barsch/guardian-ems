"""Fail closed on an older local application schema; never migrate on open."""
import sqlite3
from projection_repository import Unavailable


def require_schema(owner, connection, schema, tables):
    if getattr(owner, '_application_schema_verified', False):
        return
    reference = sqlite3.connect(':memory:')
    try:
        reference.executescript(schema)
        for table in tables:
            for pragma in ('table_info', 'foreign_key_list'):
                actual = [tuple(row) for row in connection.execute(f'PRAGMA {pragma}({table})')]
                expected = [tuple(row) for row in reference.execute(f'PRAGMA {pragma}({table})')]
                if actual != expected:
                    raise Unavailable(f'application schema {table} requires explicit migration')
            for name, sql in reference.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)):
                actual = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=? AND tbl_name=?", (name,table)).fetchone()
                if actual is None or ' '.join(actual[0].split()) != ' '.join(sql.split()):
                    raise Unavailable(f'application schema index {name} requires explicit migration')
    finally:
        reference.close()
    owner._application_schema_verified = True
