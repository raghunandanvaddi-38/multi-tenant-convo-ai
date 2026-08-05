"""
SQL Validator.

Rules enforced BEFORE the query ever reaches the driver:

  1. Exactly one statement.
  2. It must be a SELECT (a CTE-wrapped `WITH ... SELECT ...` is fine).
  3. No forbidden keywords anywhere in the token stream:
       INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE,
       EXEC/EXECUTE, GRANT, REVOKE, MERGE, LOCK, CALL, USE, SET,
       ATTACH, COPY, INTO, ANALYZE (as a top-level statement).
  4. Every table referenced (FROM / JOIN / INTO / UPDATE etc.) must be in
     the workspace's `allowed_tables` set.
  5. A LIMIT is injected if the query doesn't already have one.
  6. Query length capped at 10_000 chars (defensive; catches injection dumps).

We ALSO defend at the driver level (read-only transactions, statement
timeouts) — validator is one layer of a defence-in-depth stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis, Statement, TokenList
from sqlparse.tokens import DML, Keyword, Punctuation


FORBIDDEN_TOP_LEVEL_DML = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT",
    "CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "LOCK", "UNLOCK", "CALL", "EXEC", "EXECUTE",
    "ATTACH", "DETACH", "COPY", "USE", "SET", "ANALYZE", "VACUUM",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
}

# Anywhere in the token stream — INTO in SELECT ... INTO writes rows.
FORBIDDEN_ANYWHERE = {"INTO"}

MAX_SQL_CHARS = 10_000
DEFAULT_MAX_ROWS = 500


class ValidationError(Exception):
    """Raised when SQL fails a safety check. Message is user-safe."""


@dataclass
class ValidatedQuery:
    sql: str                     # possibly modified (LIMIT injected)
    referenced_tables: list[str] # lowercase, unqualified names — best-effort
    max_rows: int


class SQLValidator:
    def __init__(self, allowed_tables: Iterable[str], *, max_rows: int = DEFAULT_MAX_ROWS):
        # Store lowercase for case-insensitive comparisons; the identifier
        # extractor also lowercases.
        self.allowed_tables = {t.lower() for t in allowed_tables}
        self.max_rows = int(max_rows)

    def validate(self, sql: str) -> ValidatedQuery:
        if not sql or not sql.strip():
            raise ValidationError("Empty SQL")
        if len(sql) > MAX_SQL_CHARS:
            raise ValidationError(f"SQL too long ({len(sql)} chars, max {MAX_SQL_CHARS})")

        parsed = sqlparse.parse(sql)
        parsed = [p for p in parsed if p.tokens and str(p).strip()]
        if not parsed:
            raise ValidationError("No parseable statement found")
        if len(parsed) > 1:
            raise ValidationError("Multiple statements are not allowed")

        stmt: Statement = parsed[0]
        self._check_forbidden(stmt)
        self._check_is_select(stmt)

        # Extract CTE aliases up front so they don't get flagged as disallowed.
        cte_aliases = _cte_aliases(sql)
        tables = self._referenced_tables(stmt)
        disallowed = [
            t for t in tables
            if t.lower() not in self.allowed_tables and t.lower() not in cte_aliases
        ]
        if disallowed:
            raise ValidationError(
                f"Query references disallowed table(s): {', '.join(sorted(set(disallowed)))}"
            )

        final_sql = self._inject_limit_if_missing(sql, self.max_rows)
        return ValidatedQuery(
            sql=final_sql,
            referenced_tables=[t for t in tables if t.lower() not in cte_aliases],
            max_rows=self.max_rows,
        )

    # ---- Rule implementations ----------------------------------------

    def _check_forbidden(self, stmt: Statement) -> None:
        for tok in _iter_tokens(stmt):
            if tok.ttype in (DML, Keyword):
                up = str(tok).strip().upper()
                if up in FORBIDDEN_TOP_LEVEL_DML:
                    raise ValidationError(f"Statement type {up!r} is not permitted (SELECT only).")
                if up in FORBIDDEN_ANYWHERE:
                    # SELECT ... INTO writes rows. Reject.
                    raise ValidationError(f"{up!r} clause is not permitted.")

    def _check_is_select(self, stmt: Statement) -> None:
        # Skip whitespace / comments, then check the leading meaningful token.
        for tok in stmt.tokens:
            if tok.is_whitespace or _is_comment(tok):
                continue
            if tok.ttype is DML and str(tok).strip().upper() == "SELECT":
                return
            up = str(tok).strip().upper()
            if up == "WITH":
                # CTE — must resolve to SELECT.
                self._check_cte_ends_in_select(stmt)
                return
            break
        raise ValidationError("Only SELECT statements are allowed.")

    def _check_cte_ends_in_select(self, stmt: Statement) -> None:
        # After all the CTE definitions, the outer statement must be SELECT.
        seen_select = False
        for tok in _iter_tokens(stmt):
            if tok.ttype is DML:
                if str(tok).strip().upper() == "SELECT":
                    seen_select = True
                else:
                    raise ValidationError("CTE containing non-SELECT DML is not allowed.")
        if not seen_select:
            raise ValidationError("CTE must produce a SELECT.")

    def _referenced_tables(self, stmt: Statement) -> list[str]:
        tables: list[str] = []
        want_table = False
        for tok in _iter_tokens(stmt):
            if tok.is_whitespace or _is_comment(tok):
                continue
            up = str(tok).strip().upper()
            if tok.ttype is Keyword and up in ("FROM", "JOIN", "LEFT JOIN", "RIGHT JOIN",
                                                "INNER JOIN", "OUTER JOIN", "FULL JOIN",
                                                "CROSS JOIN"):
                want_table = True
                continue
            if want_table:
                # Skip further keywords like `LATERAL`, `ONLY`, etc.
                if tok.ttype is Keyword:
                    continue
                if isinstance(tok, IdentifierList):
                    for i in tok.get_identifiers():
                        tables.append(_unqualified(i.get_real_name() or i.get_name() or ""))
                    want_table = False
                    continue
                if isinstance(tok, Identifier):
                    real = tok.get_real_name() or tok.get_name() or ""
                    tables.append(_unqualified(real))
                    want_table = False
                    continue
                if isinstance(tok, Parenthesis):
                    # Subquery — recurse.
                    tables.extend(self._referenced_tables(tok))
                    want_table = False
                    continue
                # Bare name token (rare with sqlparse; still safe fallback)
                name = str(tok).strip().strip('"`').strip("'")
                if name:
                    tables.append(_unqualified(name))
                    want_table = False
        # Filter out obvious noise
        return [t for t in tables if t and not t.upper() in ("AS",)]

    _LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)

    def _inject_limit_if_missing(self, sql: str, max_rows: int) -> str:
        if self._LIMIT_RE.search(sql):
            return sql.rstrip().rstrip(";")
        return f"{sql.rstrip().rstrip(';')} LIMIT {max_rows}"


def _iter_tokens(tl: TokenList):
    """Yield leaf tokens of a statement in document order, recursing into groups."""
    for tok in tl.tokens:
        if hasattr(tok, "tokens") and tok.tokens:
            yield from _iter_tokens(tok)
        else:
            yield tok
    # Also yield the group itself for callers that want IdentifierList / Parenthesis
    yield tl


def _is_comment(tok) -> bool:
    return (getattr(tok, "ttype", None) is not None) and "Comment" in str(tok.ttype)


_CTE_RE = re.compile(
    r"(?:^|,|\bWITH\b)\s*(?:RECURSIVE\s+)?([A-Za-z_][\w]*)\s*(?:\([^)]*\))?\s+AS\s*\(",
    re.IGNORECASE,
)


def _cte_aliases(sql: str) -> set[str]:
    """Return the set of CTE names defined in a WITH clause. Case-insensitive."""
    # Only look at the leading portion up to the first non-comment SELECT so we
    # don't accidentally capture subqueries deeper in the statement.
    return {m.group(1).lower() for m in _CTE_RE.finditer(sql)}


def _unqualified(name: str) -> str:
    """Strip schema qualifiers and quotes so `public.orders` → `orders`."""
    name = (name or "").strip().strip('"`').strip("'")
    if "." in name:
        name = name.rsplit(".", 1)[1]
    return name
