#!/usr/bin/env python3
"""
Migrate code index from DuckDB to NebulaGraph.

Reads symbols and edges from DuckDB graph.duck and creates corresponding
vertices and edges in NebulaGraph.
"""

import duckdb
import os
import sys
from pathlib import Path
from typing import Any
from nebula3.gclient.net import ConnectionPool
from nebula3.Config import Config

# NebulaGraph config
NEBULA_HOST = "127.0.0.1"
NEBULA_PORT = 9669
NEBULA_USER = "root"
NEBULA_PASSWORD = "nebula"
SPACE_NAME = "agentalloy"

# DuckDB config - read from environment or use default
DUCKDB_PATH = os.environ.get("MIGRATE_DUCKDB_PATH", ".agentalloy/test-repo/graph.duck")


def escape_nGQL(s: Any) -> str:
    """Escape a value for nGQL string literals."""
    if s is None:
        return "NULL"
    s = str(s)
    # Escape backslashes, quotes, and control characters
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    s = s.replace("\t", "\\t")
    return f'"{s}"'


def migrate_symbols(duck_conn: duckdb.DuckDBPyConnection, session) -> dict[str, str]:
    """
    Migrate symbols from DuckDB to NebulaGraph.
    
    Returns a mapping of qualified_name → vertex ID (same as qualified_name).
    """
    print("\n=== Migrating Symbols ===")
    
    # Read all symbols from DuckDB
    symbols = duck_conn.execute("""
        SELECT 
            qualified_name, kind, name, file_path, start_line, end_line,
            docstring, decorators, is_exported, is_async, is_generator,
            source_code, content_hash
        FROM symbols
    """).fetchall()
    
    print(f"  Found {len(symbols)} symbols in DuckDB")
    
    # Read centrality data (pagerank scores)
    try:
        centrality = dict(duck_conn.execute("""
            SELECT qualified_name, pagerank FROM centrality
        """).fetchall())
    except Exception:
        centrality = {}
        print("  Note: No centrality table found, pagerank will be null")
    
    # Batch insert symbols
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        values = []
        
        for sym in batch:
            (qualified_name, kind, name, file_path, start_line, end_line,
             docstring, decorators, is_exported, is_async, is_generator,
             source_code, content_hash) = sym
            
            pagerank = centrality.get(qualified_name)
            
            # Build vertex value tuple
            value = f"""(
                {escape_nGQL(kind)},
                {escape_nGQL(name)},
                {escape_nGQL(file_path)},
                {start_line if start_line else 'NULL'},
                {end_line if end_line else 'NULL'},
                {escape_nGQL(docstring)},
                {escape_nGQL(str(decorators) if decorators else 'NULL')},
                {'true' if is_exported else 'false'},
                {'true' if is_async else 'false'},
                {'true' if is_generator else 'false'},
                {escape_nGQL(source_code)},
                {escape_nGQL('')},
                {escape_nGQL(content_hash)},
                {pagerank if pagerank else 'NULL'}
            )"""
            values.append(f'{escape_nGQL(qualified_name)}: {value}')
        
        # Batch INSERT
        sql = f"""
            INSERT VERTEX Symbol(
                kind, name, file_path, start_line, end_line, docstring,
                decorators, is_exported, is_async, is_generator,
                source_code, contextual_prefix, content_hash, pagerank
            ) VALUES {', '.join(values)}
        """
        
        try:
            session.execute(sql)
        except Exception as e:
            print(f"  ✗ Failed to insert batch {i//batch_size + 1}")
            print(f"    Error: {e}")
        
        if (i + batch_size) % 100 == 0:
            print(f"  Migrated {min(i + batch_size, len(symbols))}/{len(symbols)} symbols...")
    
    print(f"  ✓ Migrated {len(symbols)} symbols")
    return {sym[0]: sym[0] for sym in symbols}  # qualified_name → vertex ID


def migrate_edges(duck_conn: duckdb.DuckDBPyConnection, session, rid_map: dict[str, str]):
    """
    Migrate edges from DuckDB to NebulaGraph.
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
    
    # Map DuckDB kind to NebulaGraph edge type
    kind_to_edge_type = {
        "CALLS": "Calls",
        "IMPORTS": "Imports",
        "INHERITS": "Inherits",
        "IMPLEMENTS": "Implements",
        "OVERRIDES": "Overrides",
        "DEFINES": "Defines",
        "DEFINES_METHOD": "Defines_method",
        "RE_EXPORTS": "Re_exports",
        "CONTAINS": "HasMember",
        "GOVERNS": "Governs",
        "REQUIRES": "Requires",
        "TOUCHES": "Touches",
        "CONSTRAINTS": "Constraints",
        "COMMAND": "Command",
        "STAKEHOLDER": "Stakeholder",
        "DEPENDS_ON_EXTERNAL": "Depends_on_external",
    }
    
    # Migrate edges by kind
    total_migrated = 0
    for kind, kind_edges in edges_by_kind.items():
        edge_type = kind_to_edge_type.get(kind)
        if not edge_type:
            print(f"  ⚠ Unknown edge kind: {kind}, skipping")
            continue
        
        print(f"\n  Migrating {len(kind_edges)} {kind} edges...")
        
        # Batch insert edges
        batch_size = 100
        for i in range(0, len(kind_edges), batch_size):
            batch = kind_edges[i:i+batch_size]
            values = []
            
            for src, dst, kind, confidence, resolved_via, file_path, line_start in batch:
                # Skip edges with missing endpoints
                if src not in rid_map or dst not in rid_map:
                    continue
                
                # Build edge value tuple based on edge type
                if edge_type in ["Calls"]:
                    value = f"({confidence}, {escape_nGQL(resolved_via)}, {escape_nGQL(file_path)}, {line_start if line_start else 'NULL'})"
                elif edge_type in ["Imports", "Re_exports"]:
                    value = f"({confidence}, {escape_nGQL(resolved_via)})"
                elif edge_type in ["Inherits", "Implements", "Overrides", "Defines", "Defines_method", "HasMember"]:
                    value = f"({confidence})"
                elif edge_type == "Governs":
                    value = f"({escape_nGQL('')})"
                else:
                    value = "()"
                
                values.append(f'{escape_nGQL(src)} -> {escape_nGQL(dst)}: {value}')
            
            if not values:
                continue
            
            # Build property list based on edge type
            if edge_type == "Calls":
                props = "(confidence, resolved_via, file_path, line_start)"
            elif edge_type in ["Imports", "Re_exports"]:
                props = "(confidence, resolved_via)"
            elif edge_type in ["Inherits", "Implements", "Overrides", "Defines", "Defines_method", "HasMember"]:
                props = "(confidence)"
            elif edge_type == "Governs":
                props = "(resolution_tier)"
            else:
                props = "()"
            
            # Batch INSERT
            sql = f"INSERT EDGE {edge_type} {props} VALUES {', '.join(values)}"
            
            try:
                session.execute(sql)
            except Exception as e:
                print(f"    ✗ Failed to insert batch {i//batch_size + 1}")
                print(f"      Error: {str(e)[:200]}")
        
        migrated = len([e for e in kind_edges if e[0] in rid_map and e[1] in rid_map])
        print(f"    ✓ Migrated {migrated}/{len(kind_edges)} {kind} edges")
        total_migrated += migrated
    
    print(f"\n  ✓ Total migrated: {total_migrated} edges")


def main():
    """Main entry point."""
    print("=" * 60)
    print("DuckDB → NebulaGraph Migration")
    print("=" * 60)
    
    # Check DuckDB file exists
    duck_path = Path(DUCKDB_PATH)
    if not duck_path.exists():
        print(f"\n✗ DuckDB file not found: {DUCKDB_PATH}")
        sys.exit(1)
    
    print(f"\nDuckDB: {DUCKDB_PATH}")
    print(f"NebulaGraph: {NEBULA_HOST}:{NEBULA_PORT}/{SPACE_NAME}")
    
    # Connect to DuckDB
    print("\nConnecting to DuckDB...")
    duck_conn = duckdb.connect(str(duck_path), read_only=True)
    
    # Connect to NebulaGraph
    print("Connecting to NebulaGraph...")
    config = Config()
    config.max_connection_pool_size = 10
    pool = ConnectionPool()
    if not pool.init([(NEBULA_HOST, NEBULA_PORT)], config):
        print(f"\n✗ Failed to connect to NebulaGraph at {NEBULA_HOST}:{NEBULA_PORT}")
        sys.exit(1)
    
    session = pool.get_session(NEBULA_USER, NEBULA_PASSWORD)
    
    try:
        # Use the space
        session.execute(f"USE {SPACE_NAME}")
        print("✓ Connected to NebulaGraph")
        
        # Migrate symbols
        rid_map = migrate_symbols(duck_conn, session)
        
        # Migrate edges
        migrate_edges(duck_conn, session, rid_map)
        
        print("\n" + "=" * 60)
        print("Migration complete!")
        print("=" * 60)
        
    finally:
        session.release()
        pool.close()
        duck_conn.close()


if __name__ == "__main__":
    main()
