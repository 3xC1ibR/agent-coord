"""Transfer stored SQLite rows without a Python GIL handoff for every row."""
from __future__ import annotations

import json
import sqlite3


# Older SQLite decimal conversion can round extreme finite doubles by one ULP.
# The ordinary transfer remains exact on those supported legacy runtimes.
NATIVE_JSON_REAL_ROUND_TRIP = sqlite3.sqlite_version_info >= (3, 53, 4)


def _ordinary_rows(db, sql, parameters, columns=None):
    cursor = db.execute(sql, parameters)
    try:
        names = columns or tuple(column[0] for column in cursor.description)
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def read_rows(db, sql, parameters=(), *, columns=None):
    """Return dictionaries for a SELECT, preserving its order and scalar types.

    Callers own query bounds and snapshot lifetime. Explicit result column
    names avoid a metadata query; otherwise SQLite discovers them from the
    original query. Result column names must be unambiguous in SQLite.

    REAL values use round-trip decimal strings instead of SQLite JSON's
    shorter numeric representation. A real zero requires ordinary row transfer
    because SQLite's text formatting discards the sign of negative zero.
    """
    sql = sql.rstrip().removesuffix(";")
    if columns is None:
        # An outer SELECT renames duplicate aliases, concealing an ambiguous
        # projection. Inspect the original description and reject it instead.
        cursor = db.execute(sql, parameters)
        try:
            columns = tuple(column[0] for column in cursor.description)
        finally:
            cursor.close()
    else:
        columns = tuple(columns)
    if not columns or len({name.casefold() for name in columns}) != len(columns):
        raise ValueError("Batched SQLite results require unique column names.")
    if not NATIVE_JSON_REAL_ROUND_TRIP:
        return _ordinary_rows(db, sql, parameters, columns)
    expressions = []
    for name in columns:
        field = '"' + name.replace('"', '""') + '"'
        expressions.append(
            f"CASE typeof({field}) "
            f"WHEN 'real' THEN json_array('real', printf('%!.26g', {field})) "
            f"WHEN 'blob' THEN json_array('blob', hex({field})) "
            # Concatenation clears JSON subtype as well as preserving strings
            # that happen to contain serialized arrays or type markers.
            f"WHEN 'text' THEN {field} || '' ELSE {field} END")
    cursor = db.execute("SELECT json_group_array(json_array(" + ",".join(expressions)
                        + ")) FROM (" + sql + ")", parameters)
    try:
        packed = cursor.fetchone()[0]
    finally:
        cursor.close()
    result = []
    for values in json.loads(packed):
        row = {}
        for name, value in zip(columns, values):
            if isinstance(value, list):
                kind, encoded = value
                if kind == "real":
                    value = float(encoded)
                    if value == 0.0:
                        # Neither printf nor SQLite JSON preserves -0.0. Keep
                        # generic SELECTs exact, including future untyped
                        # columns. Callers retain any required read snapshot.
                        return _ordinary_rows(db, sql, parameters, columns)
                elif kind == "blob":
                    value = bytes.fromhex(encoded)
                else:
                    raise ValueError("Unknown SQLite scalar encoding.")
            row[name] = value
        result.append(row)
    return result
