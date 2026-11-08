from __future__ import annotations

import math
import random
import sqlite3
import struct
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import sqlite_rows
from agent_coord.sqlite_rows import read_rows


class SQLiteRowTransferTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def ordinary(self, sql, parameters=()):
        cursor = self.db.execute(sql, parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def test_storage_types_and_text_are_preserved(self):
        self.db.execute('CREATE TABLE sample (id INTEGER, "value field")')
        values = [None, 0, -2**63, 2**63-1, 0.1, math.nextafter(1.0, 2.0),
                  "", "Unicode 雪 🌱\x00tail", '["real","3.5"]', '["blob","abcd"]',
                  b"", bytes(range(256))]
        self.db.executemany('INSERT INTO sample VALUES (?, ?)', enumerate(values))
        sql = 'SELECT id, "value field" FROM sample ORDER BY id DESC'
        actual = read_rows(self.db, sql)
        self.assertEqual(actual, self.ordinary(sql))
        self.assertEqual([type(row['value field']) for row in actual], [type(value) for value in reversed(values)])

    def test_json_subtypes_remain_text(self):
        sql = "SELECT json_array('real', '3.5') AS encoded, json_object('a', 1) AS object_text"
        self.assertEqual(read_rows(self.db, sql), self.ordinary(sql))
        self.assertTrue(all(isinstance(value, str) for value in read_rows(self.db, sql)[0].values()))

    def test_real_values_round_trip_exactly(self):
        self.db.execute('CREATE TABLE sample (id INTEGER, value REAL)')
        values = [0.1, -0.1, math.nextafter(1.0, 2.0), math.nextafter(1.0, 0.0),
                  sys.float_info.min, sys.float_info.max, math.ulp(0.0),
                  -sys.float_info.min, -sys.float_info.max, -math.ulp(0.0),
                  1793741954.31393, math.inf, -math.inf]
        random_source = random.Random(73)
        while len(values) < 1013:
            value = struct.unpack('!d', random_source.getrandbits(64).to_bytes(8, 'big'))[0]
            if math.isfinite(value) and value != 0.0:
                values.append(value)
        self.db.executemany('INSERT INTO sample VALUES (?, ?)', enumerate(values))
        sql = 'SELECT value FROM sample ORDER BY id'
        self.assertEqual([row['value'].hex() for row in read_rows(self.db, sql)],
                         [row['value'].hex() for row in self.ordinary(sql)])

    def test_computed_and_untyped_negative_zero_remain_exact(self):
        self.db.execute('CREATE TABLE sample (value)')
        self.db.execute('INSERT INTO sample VALUES (?)', (-0.0,))
        for sql, parameters in [('SELECT value FROM sample', ()), ('SELECT ? AS value', (-0.0,)),
                                ('SELECT ? AS value', (0.0,))]:
            with self.subTest(sql=sql, parameters=parameters):
                self.assertEqual(read_rows(self.db, sql, parameters)[0]['value'].hex(),
                                 self.ordinary(sql, parameters)[0]['value'].hex())

    def test_parameters_empty_results_and_all_rows_are_preserved(self):
        self.db.execute('CREATE TABLE sample (id INTEGER, value TEXT)')
        self.db.executemany('INSERT INTO sample VALUES (?, ?)', ((value, str(value)) for value in range(2051)))
        sql = 'SELECT id, value FROM sample WHERE id >= :minimum ORDER BY id DESC;'
        self.assertEqual(read_rows(self.db, sql, {'minimum': 3}, columns=('id', 'value')),
                         self.ordinary(sql, {'minimum': 3}))
        self.assertEqual(len(read_rows(self.db, sql, {'minimum': 0})), 2051)
        self.assertEqual(read_rows(self.db, sql, {'minimum': 3000}), [])

    def test_transfers_one_native_result_row_without_per_row_callbacks(self):
        self.db.execute('CREATE TABLE sample (id INTEGER, value REAL)')
        self.db.executemany('INSERT INTO sample VALUES (?, ?)', ((value, value + .125) for value in range(500)))
        transferred = []

        def observe_row(cursor, row):
            transferred.append(1)
            return row

        self.db.row_factory = observe_row
        self.assertEqual(len(read_rows(self.db, 'SELECT * FROM sample', columns=('id', 'value'))), 500)
        self.assertEqual(len(transferred), 1 if sqlite_rows.NATIVE_JSON_REAL_ROUND_TRIP else 500)

    def test_legacy_real_conversion_uses_exact_native_transfer(self):
        with patch.object(sqlite_rows, 'NATIVE_JSON_REAL_ROUND_TRIP', False):
            self.test_real_values_round_trip_exactly()

    def test_ambiguous_column_names_are_rejected(self):
        for columns in (None, ('value', 'value'), ('value', 'VALUE')):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                read_rows(self.db, 'SELECT 1 AS value, 2 AS VALUE', columns=columns)


if __name__ == '__main__':
    unittest.main()
