"""The warehouse interface: SQL in, rows out, with a statement ID for the trace.

Implementations (run.warehouse_from picks one from --warehouse):
- sqlite:PATH   SqliteWarehouse, the synthetic warehouse at home.
- cmd:COMMAND   CommandWarehouse: an existing command (for example the script of the team's
                Databricks skill) gets the SQL on stdin and prints the result as JSON.
- handoff:DIR   HandoffWarehouse: for a warehouse that only the chat agent can reach (a
                Databricks MCP tool). Kangaroo writes each statement to DIR/pending/; the agent
                runs it with the tool and saves the output, unchanged, in DIR/results/.
Kangaroo never needs its own warehouse client. Every cut row has a checksum column (chk), so a
result that was copied with a changed number is refused.

Every statement is read-only (SELECT or WITH). The guard refuses anything else before it
reaches a warehouse, so a template or a lesson can never write.
"""

import glob
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess

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


class Pending(QueryRefused):
    """A statement waits for the chat agent to run it (hand-off)."""


def _num(value):
    if isinstance(value, str) and re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", value.strip()):
        return float(value) if re.search(r"[.eE]", value) else int(value)
    return value


def rows_from_json(doc):
    """Rows from a JSON result: a list of objects, {"rows": [...]}, or the Databricks SQL
    statement API shape (manifest.schema.columns and result.data_array, values as strings)."""
    if isinstance(doc, dict) and "manifest" in doc:
        cols = [c["name"] for c in doc["manifest"]["schema"]["columns"]]
        rows = [dict(zip(cols, r)) for r in (doc.get("result") or {}).get("data_array") or []]
    elif isinstance(doc, dict) and "rows" in doc:
        rows = doc["rows"]
    elif isinstance(doc, list):
        rows = doc
    else:
        raise QueryRefused("the result is not a list of rows, {\"rows\": [...]} or a Databricks statement result")
    return [{k: _num(v) for k, v in r.items()} for r in rows]


CHECKSUM = (("n", 1), ("sx", 1), ("sy", 2), ("sxx", 3), ("syy", 5), ("sxy", 7))


def verify(rows):
    """Refuse a result whose chk column does not match its numbers (a value was changed)."""
    for r in rows:
        if r.get("chk") is None:
            continue
        want = sum(w * float(r.get(k) or 0) for k, w in CHECKSUM)
        if abs(float(r["chk"]) - want) > 1e-6 * max(1.0, abs(want)):
            raise QueryRefused("a result row does not match its checksum: save the tool output again, without changes")
    return rows


class CommandWarehouse:
    """Runs an existing command for each statement: the finished SQL on stdin, JSON on stdout."""

    def __init__(self, command, dialect="spark", timeout=900):
        self.command, self.dialect, self.timeout, self.count = shlex.split(command), dialect, timeout, 0

    def query(self, sql, params=None, label=""):
        guard(sql)
        finished = bind(sql, params, "spark")[0]
        self.count += 1
        try:
            done = subprocess.run(self.command, input=finished, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as err:
            raise TimeoutError(f"the warehouse command took more than {self.timeout} s: {label}") from err
        if done.returncode != 0:
            raise QueryRefused(f"the warehouse command failed ({done.returncode}): {done.stderr.strip()[-200:]}")
        doc = json.loads(done.stdout)
        rows = verify(rows_from_json(doc))
        sid = (doc.get("statement_id") if isinstance(doc, dict) else None) or f"cmd-{self.count:04d}-{statement_sha(sql, params)}"
        return {"statement_id": sid, "label": label, "rows": rows, "row_count": len(rows), "sql_sha": statement_sha(sql, params)}


class HandoffWarehouse:
    """For a warehouse that only the chat agent can reach. A statement without a result is
    written to DIR/pending/<id>.sql and raises Pending (the run records an access gap and
    goes on). After the agent saves DIR/results/<id>.json, the next run reads it."""

    def __init__(self, folder, dialect="spark"):
        self.folder, self.dialect = folder, dialect
        for sub in ("pending", "results"):
            os.makedirs(os.path.join(folder, sub), exist_ok=True)

    def query(self, sql, params=None, label=""):
        guard(sql)
        sha = statement_sha(sql, params)
        result = os.path.join(self.folder, "results", f"{sha}.json")
        if os.path.exists(result):
            with open(result, encoding="utf-8") as handle:
                doc = json.load(handle)
            rows = verify(rows_from_json(doc))
            sid = (doc.get("statement_id") if isinstance(doc, dict) else None) or f"handoff-{sha}"
            return {"statement_id": sid, "label": label, "rows": rows, "row_count": len(rows), "sql_sha": sha}
        with open(os.path.join(self.folder, "pending", f"{sha}.sql"), "w", encoding="utf-8") as handle:
            handle.write(f"-- {label}\n{bind(sql, params, 'spark')[0]}\n")
        raise Pending(f"waiting for results: {len(self.pending())} statements in {self.folder}/pending")

    def pending(self):
        return sorted(p for p in glob.glob(os.path.join(self.folder, "pending", "*.sql"))
                      if not os.path.exists(os.path.join(self.folder, "results", os.path.basename(p)[:-4] + ".json")))


class SplitWarehouse:
    """T1 statements to one warehouse (a scorecard file), every other statement to another."""

    def __init__(self, t1, rest, t1_table):
        self.t1, self.rest, self.t1_table, self.dialect = t1, rest, t1_table, rest.dialect

    def query(self, sql, params=None, label=""):
        return (self.t1 if re.search(rf"\b{re.escape(self.t1_table)}\b", sql) else self.rest).query(sql, params, label)


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

