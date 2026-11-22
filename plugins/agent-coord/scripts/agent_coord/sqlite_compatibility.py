"""SQLite interfaces shared by all supported Python versions."""
from __future__ import annotations

import sqlite3
import sys


# SQLite result codes are stable, but Python exposes their names only from 3.11.
SQLITE_BUSY = getattr(sqlite3, "SQLITE_BUSY", 5)
SQLITE_CANTOPEN = getattr(sqlite3, "SQLITE_CANTOPEN", 14)


def clear_authorizer(connection):
    """Restore unrestricted statements after a finite read-only scope.

    Python 3.10 accepts None but installs it as a non-callable authorizer,
    causing subsequent statements and transaction completion to be denied.
    """
    connection.set_authorizer(None if sys.version_info >= (3, 11) else _allow_statement)


def _allow_statement(*_):
    return sqlite3.SQLITE_OK
