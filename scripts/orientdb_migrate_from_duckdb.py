#!/usr/bin/env python3
"""
Migrate code index from DuckDB to OrientDB.

Reads symbols and edges from DuckDB graph.duck and creates corresponding
vertices and edges in OrientDB.
"""

import duckdb
import httpx
import os
import sys
from pathlib import Path
from typing import Any, Optional

# OrientDB config
ORIENTDB_URL = "http://localhost:2481"
# Derive database name from DuckDB path (slug from repos/{slug}/...)
_duck_path = os.environ.get("MIGRATE_DUCKDB_PATH", ".agentalloy/test-repo/graph.duck")
_path_parts = Path(_duck_path).parts
_slug = "test_repo"
try:
    _repos_idx = _path_parts.index("repos")
    if _repos_idx + 1 < len(_path_parts):
        _slug = _path_parts[_repos_idx + 1].replace("-", "_").replace(".", "_")
except ValueError:
    pass
DATABASE = os.environ.get("MIGRATE_ORIENTDB_DATABASE", _slug)
USERNAME = "admin"
PASSWORD = "admin"

# DuckDB config
DUCKDB_PATH = _duck_path


def orient_command(sql: str, params: list[Any] | None = None) -> dict:
    """Execute a SQL command against OrientDB."""
    url = f"{ORIENTDB_URL}/command/{DATABASE}/sql"
    payload = {"command": sql, "parameters": params or []}
    response = httpx.post(
        url,
        json=payload,
        auth=(USERNAME, PASSWORD),
        timeout=30.0
    )
    if response.status_code != 200:
        print(f"  ✗ Error: {response.status_code}")
        print(f"    SQL: {sql[:100]}")
        print(f"    Response: {response.text[:200]}")
    response.raise_for_status()
    return response.json()


def orient_query(sql: str, params: list[Any] | None = None) -> list[dict]:
    """Execute a SQL query against OrientDB."""
    url = f"{ORIENTDB_URL}/query/{DATABASE}/sql"
    payload = {"query": sql, "parameters": params or []}
    response = httpx.post(
        url,
        json=payload,
        auth=(USERNAME, PASSWORD),
        timeout=30.0
    )
    response.raise_for_status()
    return response.json().get("result", [])


def migrate_symbols(duck_conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """
    Migrate symbols from DuckDB to OrientDB.
    
    Returns a mapping of qualified_name → OrientDB RID.
    """
    print("\n=== Migrating Symbols ===")
    
    # Read all symbols from DuckDB
    symbols = duck_conn.execute("""
        SELECT 
            qualified_name, kind, name, file_path, start_line, end_line,
            docstring, is_exported, is_async, is_generator, source_code,
            content_hash, pagerank
        FROM symbols
    """).fetchall()
    
    print(f"  Found {len(symbols)} symbols in DuckDB")
    
    # Mapping of qualified_name → OrientDB RID
    rid_map = {}
    
    for i, sym in enumerate(symbols, 1):
        (qualified_name, kind, name, file_path, start_line, end_line,
         docstring, is_exported, is_async, is_generator, source_code,
         content_hash, pagerank) = sym
        
        # Escape single quotes in strings
        def escape(s):
            if s is None:
                return "null"
            return "'" + str(s).replace("'", "''") + "'"
        
        # Build INSERT statement
        sql = f"""
            INSERT INTO Symbol SET
                qualified_name = {escape(qualified_name)},
                kind = {escape(kind)},
                name = {escape(name)},
                file_path = {escape(file_path)},
                start_line = {start_line if start_line else 'null'},
                end_line = {end_line if end_line else 'null'},
                docstring = {escape(docstring)},
                is_exported = {'true' if is_exported else 'false'},
                is_async = {'true' if is_async else 'false'},
                is_generator = {'true' if is_generator else 'false'},
                source_code = {escape(source_code)},
                content_hash = {escape(content_hash)},
                pagerank = {pagerank if pagerank else 'null'}
        """
        
        try:
            result = orient_command(sql)
            rid = result.get("result", [{}])[0].get("@rid")
            if rid:
                rid_map[qualified_name] = rid
        except Exception as e:
            print(f"  ✗ Failed to insert symbol: {qualified_name}")
            print(f"    Error: {e}")
        
        if i % 100 == 0:
            print(f"  Migrated {i}/{len(symbols)} symbols...")
    
    print(f"  ✓ Migrated {len(rid_map)} symbols")
    return rid_map


def migrate_edges(duck_conn: duckdb.DuckDBPyConnection, rid_map: dict[str, str]):
    """
    Migrate edges from DuckDB to OrientDB.
    
    Uses the rid_map to resolve qualified_name → RID for edge endpoints.
    """
    print("\n=== Migrating Edges ===")
    
    # Read all edges from DuckDB
    edges = duck_conn.execute("""
        SELECT src, dst, kind, confidence, resolved_via, file_path, line_start
        FROM edges
    """).fetchall()
    
    print(f"  Found {len(edges)} edges in DuckDB")
    
    # Group edges by kind for batch processing
    edges_by_kind = {}
    for edge in edges:
        src, dst, kind, confidence, resolved_via, file_path, line_start = edge
        if kind not in edges_by_kind:
            edges_by_kind[kind] = []
        edges_by_kind[kind].append(edge)
    
    # Migrate edges by kind
    total_migrated = 0
    for kind, kind_edges in edges_by_kind.items():
        print(f"\n  Migrating {len(kind_edges)} {kind} edges...")
        
        # Map DuckDB kind to OrientDB edge class
        edge_class = kind.capitalize()
        if edge_class == "Contains":
            edge_class = "HasMember"  # Renamed due to reserved keyword
        
        migrated = 0
        for src, dst, kind, confidence, resolved_via, file_path, line_start in kind_edges:
            src_rid = rid_map.get(src)
            dst_rid = rid_map.get(dst)
            
            if not src_rid or not dst_rid:
                # Skip edges with missing endpoints
                continue
            
            # Build CREATE EDGE statement
            sql = f"CREATE EDGE {edge_class} FROM {src_rid} TO {dst_rid}"
            
            # Add properties if present
            props = []
            if confidence is not None:
                props.append(f"confidence = {confidence}")
            if resolved_via:
                props.append(f"resolved_via = '{resolved_via}'")
            if file_path:
                props.append(f"file_path = '{file_path}'")
            if line_start is not None:
                props.append(f"line_start = {line_start}")
            
            if props:
                sql += " SET " + ", ".join(props)
            
            try:
                orient_command(sql)
                migrated += 1
            except Exception as e:
                pass  # Skip failed edges silently
            
            if migrated % 100 == 0 and migrated > 0:
                print(f"    Migrated {migrated}/{len(kind_edges)}...")
        
        print(f"    ✓ Migrated {migrated}/{len(kind_edges)} {kind} edges")
        total_migrated += migrated
    
    print(f"\n  ✓ Total migrated: {total_migrated} edges")


def main():
    """Main entry point."""
    print("=" * 60)
    print("DuckDB → OrientDB Migration")
    print("=" * 60)
    
    # Check DuckDB file exists
    duck_path = Path(DUCKDB_PATH)
    if not duck_path.exists():
        print(f"\n✗ DuckDB file not found: {DUCKDB_PATH}")
        sys.exit(1)
    
    print(f"\nDuckDB: {DUCKDB_PATH}")
    print(f"OrientDB: {ORIENTDB_URL}/{DATABASE}")
    
    # Connect to DuckDB
    print("\nConnecting to DuckDB...")
    duck_conn = duckdb.connect(str(duck_path), read_only=True)
    
    try:
        # Migrate symbols
        rid_map = migrate_symbols(duck_conn)
        
        # Migrate edges
        migrate_edges(duck_conn, rid_map)
        
        print("\n" + "=" * 60)
        print("Migration complete!")
        print("=" * 60)
        
    finally:
        duck_conn.close()


if __name__ == "__main__":
    main()
