"""Toy session layer. Two hops below the routes — a good test of hop limits."""


class Session:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def query(self, sql: str, params: tuple = ()):
        return execute(self.dsn, sql, params)

    def query_one(self, sql: str, params: tuple = ()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, values: dict):
        cols = ", ".join(values)
        return execute(self.dsn, f"INSERT INTO {table} ({cols}) VALUES (...)", tuple(values.values()))


def execute(dsn: str, sql: str, params: tuple):
    return []


def get_session() -> Session:
    return Session("sqlite:///sample.db")
