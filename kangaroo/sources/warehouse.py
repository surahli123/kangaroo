"""The warehouse interface: SQL in, rows out, with a statement ID for the trace.

Two implementations:
- SqliteWarehouse: the synthetic warehouse at home.
- McpWarehouse: the work warehouse, reached through the HTTP MCP's read tool. Finish it at
  work (see its docstring); everything else calls only `query`.

Every statement is read-only (SELECT or WITH). The guard refuses anything else before it
reaches a warehouse, so a template or a lesson can never write.
"""

import hashlib
import json
import os
import re
import sqlite3

READ_ONLY = re.compile(r"^\s*(with|select)\b", re.I)
WRITES = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|pragma|replace|merge|grant|truncate)\b", re.I)


class QueryRefused(ValueError):
    pass


def guard(sql):
    body = re.sub(r"'(?:[^']|'')*'", "''", sql)  # ignore words inside string literals
    if not READ_ONLY.match(body) or WRITES.search(body) or ";" in body.strip().rstrip(";"):
        raise QueryRefused("only a single read-only SELECT or WITH statement may run")


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(literal(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


def bind(sql, params, dialect):
    """SQLite binds named parameters; Spark through the MCP takes a finished statement."""
    params = params or {}
    if dialect == "sqlite":
        expanded = sql
        flat = {}
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                names = [f"{key}_{i}" for i in range(len(value))]
                expanded = re.sub(rf":{key}\b", "(" + ", ".join(f":{n}" for n in names) + ")", expanded)
                flat.update(zip(names, value))
            else:
                flat[key] = value
        return expanded, flat
    return re.sub(r":(\w+)\b", lambda m: literal(params[m.group(1)]) if m.group(1) in params else m.group(0), sql), None


def statement_sha(sql, params):
    return hashlib.sha256((sql + json.dumps(params or {}, sort_keys=True, default=str)).encode()).hexdigest()[:12]


class RecordingWarehouse:
    """Wraps a warehouse and keeps every result, so a case can be replayed without it.
    Results are aggregates (scorecard rows, per-arm sums), never query-level rows."""

    def __init__(self, inner):
        self.inner, self.dialect, self.results = inner, inner.dialect, {}

    def query(self, sql, params=None, label=""):
        res = self.inner.query(sql, params, label)
        self.results[statement_sha(sql, params)] = {"label": label, "rows": res["rows"]}
        return res

    def save(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.results, handle, default=str)


class FrozenWarehouse:
    """Answers from a case's recorded results; a statement it never saw goes to `fallback`
    (the live warehouse) or raises QueryRefused. Replays stay reproducible as T1 changes."""

    def __init__(self, path, fallback=None):
        with open(path, encoding="utf-8") as handle:
            self.results = json.load(handle)
        self.fallback = fallback
        self.dialect = fallback.dialect if fallback else "sqlite"
        self.count = 0

    def query(self, sql, params=None, label=""):
        guard(sql)
        digest = statement_sha(sql, params)
        self.count += 1
        if digest in self.results:
            rows = self.results[digest]["rows"]
            return {"statement_id": f"frozen-{self.count:04d}-{digest}", "label": label, "rows": rows,
                    "row_count": len(rows), "sql_sha": digest}
        if self.fallback:
            return self.fallback.query(sql, params, label)
        raise QueryRefused(f"not in the frozen case and no live warehouse: {label}")


class SqliteWarehouse:
    dialect = "sqlite"

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.count = 0

    def close(self):
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:  # noqa: BLE001  (interpreter shutdown)
            pass

    def query(self, sql, params=None, label=""):
        guard(sql)
        text, bound = bind(sql, params, self.dialect)
        self.count += 1
        digest = statement_sha(sql, params)
        rows = [dict(r) for r in self.db.execute(text, bound).fetchall()]
        return {"statement_id": f"sqlite-{self.count:04d}-{digest}", "label": label, "rows": rows,
                "row_count": len(rows), "sql_sha": digest}


class McpWarehouse:
    """The work warehouse, through the HTTP MCP. Finish at work:

    1. `query(sql)` calls the read tool (the one that submits a statement; allow-list it by
       name, never the dashboard write tool) with the finished Spark SQL from `bind`.
    2. Poll the status tool with the returned statement ID, up to 15 minutes, then read rows.
    3. Return {"statement_id", "label", "rows", "row_count", "sql_sha"} as SqliteWarehouse does.
    4. On a timeout or a refusal, raise TimeoutError or QueryRefused; the run records an
       access gap and goes on.
    """

    dialect = "spark"

    def __init__(self, submit, status, poll_seconds=900):
        self.submit, self.status, self.poll_seconds = submit, status, poll_seconds

    def query(self, sql, params=None, label=""):
        guard(sql)
        raise NotImplementedError("McpWarehouse.query: wire the allow-listed read tool here at work")
