"""
Persist a DiscoveredSchema into the DB, preserving existing customer
descriptions and `allowed` flags where possible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db_connectors.base import DiscoveredSchema
from app.models import DBColumn, DBConnection, DBRelationship, DBTable


log = logging.getLogger("db_query.discovery")


async def persist_schema(session: AsyncSession, conn: DBConnection, discovered: DiscoveredSchema) -> dict:
    """
    Diff + merge. Returns a summary dict: {added_tables, removed_tables, changed_columns}.
    Preserves: `allowed` flag, `description` on tables and columns.
    """
    existing_tables = (
        await session.execute(
            select(DBTable).where(DBTable.connection_id == conn.id).options(selectinload(DBTable.columns))
        )
    ).scalars().all()
    existing_by_key = {(t.schema_name or "", t.table_name): t for t in existing_tables}

    discovered_keys = {(t.schema_name or "", t.table_name) for t in discovered.tables}

    added_tables, changed_cols = 0, 0

    # Remove tables not present anymore
    removed = [t for k, t in existing_by_key.items() if k not in discovered_keys]
    for t in removed:
        await session.delete(t)

    # Upsert every discovered table
    row_by_qualified = {}
    for dtbl in discovered.tables:
        key = (dtbl.schema_name or "", dtbl.table_name)
        row = existing_by_key.get(key)
        if row is None:
            row = DBTable(
                connection_id=conn.id,
                schema_name=dtbl.schema_name or "",
                table_name=dtbl.table_name,
                table_type=dtbl.table_type,
                allowed=False, description=None,
            )
            session.add(row)
            await session.flush()
            added_tables += 1
        else:
            row.table_type = dtbl.table_type
        row_by_qualified[key] = row

        # Columns
        existing_cols = {c.column_name: c for c in row.columns} if row.columns else {}
        new_col_names = {c.name for c in dtbl.columns}
        for name, c in existing_cols.items():
            if name not in new_col_names:
                await session.delete(c)
                changed_cols += 1
        for dcol in dtbl.columns:
            col = existing_cols.get(dcol.name)
            if col is None:
                col = DBColumn(
                    table_id=row.id, column_name=dcol.name, data_type=dcol.data_type,
                    is_nullable=dcol.is_nullable, is_primary_key=dcol.is_primary_key,
                    ordinal_position=dcol.ordinal_position, description=None,
                )
                session.add(col)
                changed_cols += 1
            else:
                if (col.data_type != dcol.data_type
                    or col.is_nullable != dcol.is_nullable
                    or col.is_primary_key != dcol.is_primary_key
                    or col.ordinal_position != dcol.ordinal_position):
                    col.data_type = dcol.data_type
                    col.is_nullable = dcol.is_nullable
                    col.is_primary_key = dcol.is_primary_key
                    col.ordinal_position = dcol.ordinal_position
                    changed_cols += 1

    # Replace relationships entirely — they're derived, not customer-edited
    for r in (await session.execute(select(DBRelationship).where(DBRelationship.connection_id == conn.id))).scalars():
        await session.delete(r)
    await session.flush()
    for rel in discovered.relationships:
        from_row = row_by_qualified.get((rel.from_schema or "", rel.from_table))
        to_row = row_by_qualified.get((rel.to_schema or "", rel.to_table))
        if from_row is None or to_row is None:
            continue
        session.add(DBRelationship(
            connection_id=conn.id, constraint_name=rel.constraint_name,
            from_table_id=from_row.id, from_column=rel.from_column,
            to_table_id=to_row.id, to_column=rel.to_column,
        ))

    conn.last_refreshed_at = datetime.now(timezone.utc)
    await session.commit()

    return {
        "added_tables": added_tables,
        "removed_tables": len(removed),
        "changed_columns": changed_cols,
        "total_tables": len(discovered.tables),
    }
