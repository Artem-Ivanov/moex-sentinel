"""SQLite revision 1: three state CHECKs, preserving the current stored schema."""

# Identifiers are quoted; CHECK expressions are built only from fixed enums.
# ruff: noqa: S608
import re

from sqlalchemy import Connection

from sentinel_contracts.broker_execution import BrokerOrderStatus
from sentinel_contracts.trading import AutomationState
from trading_automaton.domain.storage_dtos import OutboxDeliveryState

STATE_CHECKS = {
    "cached_automations": ("state", "ck_worker_automation_state", tuple(s.value for s in AutomationState)),
    "broker_intents": ("state", "ck_worker_order_state", tuple(s.value for s in BrokerOrderStatus)),
    "fact_outbox": ("delivery_state", "ck_worker_outbox_state", tuple(s.value for s in OutboxDeliveryState)),
}


def state_check_sql(table: str) -> str:
    column, _, values = STATE_CHECKS[table]
    literals = ", ".join(f"'{value}'" for value in values)
    return f"{column} IS NOT NULL AND {column} COLLATE BINARY IN ({literals})"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _definitions(sql: str, table: str) -> list[str]:
    # Accept ordinary tables only; unsupported SQL must fail before any DDL.
    prefix = re.match(r'CREATE\s+TABLE\s+(?:"[^"]+"|\w+)\s*\(', sql, re.I)
    if prefix is None or not sql.rstrip().endswith(")") or re.search(r"AUTOINCREMENT|--|/\*", sql, re.I):
        raise ValueError(f"Unsupported Worker table DDL: {table}")
    body = sql[prefix.end() : sql.rfind(")")]
    result, start, depth, quote = [], 0, 0, None
    i = 0
    while i < len(body):
        char = body[i]
        if quote:
            if char == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in "'\"`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(body[start:i].strip())
            start = i + 1
        if depth < 0:
            raise ValueError(f"Ambiguous Worker table DDL: {table}")
        i += 1
    if quote or depth:
        raise ValueError(f"Ambiguous Worker table DDL: {table}")
    result.append(body[start:].strip())
    return result


def migrate_state_checks(connection: Connection, *, downgrade: bool = False) -> None:
    """Caller owns BEGIN IMMEDIATE and commits schema, values and version together."""
    version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
    if version not in {0, 1} or (downgrade and version != 1):
        raise ValueError(f"Unsupported Worker schema version: {version}")
    objects = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE sql IS NOT NULL"
    ).all()
    for kind, name, _, _ in objects:
        if (
            kind in {"table", "view"}
            and name.casefold() in STATE_CHECKS
            and (kind != "table" or name not in STATE_CHECKS)
        ):
            raise ValueError(f"Ambiguous Worker state table identity: {name}")
    tables = {name: sql for kind, name, _, sql in objects if kind == "table"}
    present = set(tables) & STATE_CHECKS.keys()
    if version == 1 and present != STATE_CHECKS.keys():
        raise ValueError("Worker schema revision 1 is missing state tables")
    for kind, name, owner, sql in objects:
        if kind == "table":
            foreign_keys = connection.exec_driver_sql(f"PRAGMA foreign_key_list({_quote(name)})").all()
            if (owner in present and foreign_keys) or any(row[2].casefold() in present for row in foreign_keys):
                raise ValueError(f"Unsupported foreign key dependency: {name}")
        if kind in {"view", "trigger"} and any(
            (kind == "view" or owner != table) and re.search(r"\b" + re.escape(table) + r"\b", sql, re.I)
            for table in present
        ):
            raise ValueError(f"Unsupported external Worker schema dependency: {name}")
    rebuilds = []
    for table, (column, constraint, _) in STATE_CHECKS.items():
        if table not in present:
            continue
        definitions = _definitions(tables[table], table)
        columns = connection.exec_driver_sql(f"PRAGMA table_xinfo({_quote(table)})").all()
        state_columns = [row for row in columns if row[1] == column]
        if len(state_columns) != 1 or not re.fullmatch(r"(?:TEXT|VARCHAR(?:\(\d+\))?)", state_columns[0][2], re.I):
            raise ValueError(f"Unsupported Worker state column: {table}.{column}")
        if any(row[6] for row in columns):
            raise ValueError(f"Unsupported generated/hidden column: {table}")
        expression = state_check_sql(table)
        if (
            version == 0
            and connection.exec_driver_sql(f"SELECT 1 FROM {_quote(table)} WHERE NOT ({expression}) LIMIT 1").first()
        ):
            raise ValueError(f"Invalid historical Worker state: {table}.{column}")
        own = f"CONSTRAINT {constraint} CHECK ({expression})"
        matches = [item for item in definitions if constraint in item]
        if (matches and matches != [own]) or (version == 1 and matches != [own]):
            raise ValueError(f"Ambiguous Worker state constraint: {table}")
        if downgrade:
            definitions.remove(own)
        elif not matches:
            definitions.append(own)
        else:
            continue
        temporary = f"_worker_states_{table}"
        if any(name.casefold() == temporary for _, name, _, _ in objects):
            raise ValueError(f"Worker migration temporary name already exists: {temporary}")
        stored_columns = [row[1] for row in columns]
        rowid = next(
            (
                alias
                for alias in ("rowid", "_rowid_", "oid")
                if alias not in {name.casefold() for name in stored_columns}
            ),
            None,
        )
        if rowid is None:
            raise ValueError(f"Unsupported shadowed rowid: {table}")
        stored_columns.insert(0, rowid)
        dependents = [sql for kind, _, owner, sql in objects if owner == table and kind in {"index", "trigger"}]
        rebuilds.append((table, temporary, definitions, stored_columns, dependents))
    for table, temporary, definitions, columns, dependents in rebuilds:
        connection.exec_driver_sql(f"CREATE TABLE {_quote(temporary)} (" + ", ".join(definitions) + ")")
        names = ", ".join(_quote(name) for name in columns)
        connection.exec_driver_sql(f"INSERT INTO {_quote(temporary)} ({names}) SELECT {names} FROM {_quote(table)}")
        connection.exec_driver_sql(f"DROP TABLE {_quote(table)}")
        connection.exec_driver_sql(f"ALTER TABLE {_quote(temporary)} RENAME TO {_quote(table)}")
        for sql in dependents:
            connection.exec_driver_sql(sql)
    target_version = 0 if downgrade else 1
    if version != target_version:
        connection.exec_driver_sql(f"PRAGMA user_version={target_version}")
