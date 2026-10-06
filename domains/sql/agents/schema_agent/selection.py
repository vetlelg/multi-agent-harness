"""From the tables the model named to the schema the query agent sees.

The model only names tables. Everything else is looked up here, in code: every
column of each table, the tables that join them, and the join conditions between
them. Choosing what a question is about takes a model; how the tables connect is
in the schema, and a shortest path is not a judgement.
"""

from __future__ import annotations

from collections import deque

from domains.sql.models import TableInfo


def select_tables(schema: list[TableInfo], names: list[str]) -> list[TableInfo]:
    """The named tables, joined: every column and foreign key, in schema order.

    Names are matched ignoring case, as SQLite matches them; unknown names are
    dropped. Each named table not yet in the result is joined to it by the
    shortest foreign-key path from any table already in it, and the tables on
    that path are added.
    """
    by_name = {table.name.lower(): table.name for table in schema}
    chosen = {by_name[name.lower()] for name in names if name.lower() in by_name}
    order = [table.name for table in schema if table.name in chosen]
    if not order:
        return []

    graph = _join_graph(schema)
    connected = {order[0]}
    for name in order[1:]:
        if name not in connected:
            connected.update(_shortest_path(graph, connected, name) or [name])

    return [table for table in schema if table.name in connected]


def _join_graph(schema: list[TableInfo]) -> dict[str, list[str]]:
    """Tables as nodes, a foreign key in either direction as an edge. Neighbours sorted."""
    graph: dict[str, set[str]] = {table.name: set() for table in schema}
    for table in schema:
        for fk in table.foreign_keys:
            if fk.references_table in graph and fk.references_table != table.name:
                graph[table.name].add(fk.references_table)
                graph[fk.references_table].add(table.name)
    return {name: sorted(neighbours) for name, neighbours in graph.items()}


def _shortest_path(graph: dict[str, list[str]], start: set[str], target: str) -> list[str] | None:
    """The tables from ``start`` to ``target``, excluding ``start``; None if unreachable."""
    previous: dict[str, str | None] = dict.fromkeys(start)
    queue = deque(sorted(start))
    while queue:
        node = queue.popleft()
        if node == target:
            path = []
            while node not in start:
                path.append(node)
                node = previous[node]
            return path
        for neighbour in graph[node]:
            if neighbour not in previous:
                previous[neighbour] = node
                queue.append(neighbour)
    return None


def render_schema(tables: list[TableInfo]) -> str:
    """The tables, then the joins that can be made between them.

    ``REFERENCES`` says what a key column holds, whether or not its table is
    shown. ``Joins`` lists only the conditions between tables that are shown,
    written as they go in an ``ON``: a model copies a listed condition more
    reliably than it composes one from two ``REFERENCES`` clauses.
    """
    if not tables:
        return ""
    parts: list[str] = []
    for t in tables:
        fk_map = {fk.column: fk for fk in t.foreign_keys}
        lines: list[str] = []
        for col in t.columns:
            frags = [f"  {col.name} {col.type}"]
            if col.pk:
                frags.append("PRIMARY KEY")
            if not col.nullable:
                frags.append("NOT NULL")
            if col.name in fk_map:
                fk = fk_map[col.name]
                frags.append(f"REFERENCES {fk.references_table}({fk.references_column})")
            lines.append(" ".join(frags))
        parts.append(f"{t.name} (\n" + ",\n".join(lines) + "\n)")

    shown = {t.name for t in tables}
    joins = [
        f"  {t.name}.{fk.column} = {fk.references_table}.{fk.references_column}"
        for t in tables
        for fk in t.foreign_keys
        if fk.references_table in shown
    ]
    parts.append("Joins:\n" + "\n".join(joins) if joins else "Joins: none")
    return "\n\n".join(parts)
