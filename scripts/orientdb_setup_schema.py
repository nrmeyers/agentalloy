#!/usr/bin/env python3
"""
Set up OrientDB schema for AgentAlloy code intelligence.

Creates vertex classes (Symbol, Decision, Lesson), edge classes (Calls, Imports, etc.),
and indexes for fast traversal.
"""

import httpx
import sys
from typing import Any

ORIENTDB_URL = "http://localhost:2481"
DATABASE = "agentalloy2"
USERNAME = "admin"
PASSWORD = "admin"


def execute_command(sql: str, params: list[Any] | None = None) -> dict:
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
        print(f"SQL: {sql}")
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
    response.raise_for_status()
    return response.json()


def create_vertex_classes():
    """Create vertex classes for Symbol, Decision, and Lesson."""
    print("Creating vertex classes...")
    
    # Symbol vertex
    execute_command("""
        CREATE CLASS Symbol IF NOT EXISTS EXTENDS V
    """)
    
    # Decision vertex
    execute_command("""
        CREATE CLASS Decision IF NOT EXISTS EXTENDS V
    """)
    
    # Lesson vertex
    execute_command("""
        CREATE CLASS Lesson IF NOT EXISTS EXTENDS V
    """)
    
    print("✓ Vertex classes created")


def create_edge_classes():
    """Create edge classes for code and knowledge relationships."""
    print("Creating edge classes...")
    
    # Code edges
    code_edges = [
        "Calls",
        "Imports",
        "Inherits",
        "Implements",
        "Overrides",
        "Defines",
        "HasMember",  # Renamed from Contains (reserved keyword)
    ]
    
    for edge in code_edges:
        execute_command(f"CREATE CLASS {edge} IF NOT EXISTS EXTENDS E")
    
    # Knowledge edges
    knowledge_edges = [
        "Governs",
        "Requires",
        "Touches",
        "Constraints",
        "Command",
        "Stakeholder",
    ]
    
    for edge in knowledge_edges:
        execute_command(f"CREATE CLASS {edge} IF NOT EXISTS EXTENDS E")
    
    print("✓ Edge classes created")


def create_properties():
    """Create properties for vertex and edge classes."""
    print("Creating properties...")
    
    # Symbol properties
    symbol_props = [
        ("qualified_name", "STRING", True),
        ("kind", "STRING", True),
        ("name", "STRING", True),
        ("file_path", "STRING", False),
        ("start_line", "INTEGER", False),
        ("end_line", "INTEGER", False),
        ("docstring", "STRING", False),
        ("decorators", "EMBEDDEDLIST STRING", False),
        ("is_exported", "BOOLEAN", False),
        ("is_async", "BOOLEAN", False),
        ("is_generator", "BOOLEAN", False),
        ("source_code", "STRING", False),
        ("contextual_prefix", "STRING", False),
        ("content_hash", "STRING", False),
        ("pagerank", "FLOAT", False),
    ]
    
    for prop_name, prop_type, mandatory in symbol_props:
        mandatory_str = "MANDATORY" if mandatory else ""
        try:
            execute_command(f"""
                CREATE PROPERTY Symbol.{prop_name} {prop_type} {mandatory_str}
            """)
        except Exception:
            pass  # Property may already exist
    
    # Decision properties
    decision_props = [
        ("slug", "STRING", True),
        ("title", "STRING", False),
        ("body", "STRING", False),
        ("source_path", "STRING", False),
        ("created_at", "DATETIME", False),
        ("updated_at", "DATETIME", False),
        ("metadata", "EMBEDDEDMAP", False),
    ]
    
    for prop_name, prop_type, mandatory in decision_props:
        mandatory_str = "MANDATORY" if mandatory else ""
        try:
            execute_command(f"""
                CREATE PROPERTY Decision.{prop_name} {prop_type} {mandatory_str}
            """)
        except Exception:
            pass  # Property may already exist
    
    # Lesson properties
    lesson_props = [
        ("slug", "STRING", True),
        ("title", "STRING", False),
        ("body", "STRING", False),
        ("source_path", "STRING", False),
        ("promoted", "BOOLEAN", False),
        ("created_at", "DATETIME", False),
    ]
    
    for prop_name, prop_type, mandatory in lesson_props:
        mandatory_str = "MANDATORY" if mandatory else ""
        try:
            execute_command(f"""
                CREATE PROPERTY Lesson.{prop_name} {prop_type} {mandatory_str}
            """)
        except Exception:
            pass  # Property may already exist
    
    # Edge properties (common for all edge types)
    edge_props = [
        ("confidence", "FLOAT", False),
        ("resolved_via", "STRING", False),
        ("file_path", "STRING", False),
        ("line_start", "INTEGER", False),
    ]
    
    edge_classes = [
        "Calls", "Imports", "Inherits", "Implements", "Overrides",
        "Governs", "Requires", "Touches", "Constraints", "Command", "Stakeholder"
    ]
    
    for edge_class in edge_classes:
        for prop_name, prop_type, mandatory in edge_props:
            mandatory_str = "MANDATORY" if mandatory else ""
            try:
                execute_command(f"""
                    CREATE PROPERTY {edge_class}.{prop_name} {prop_type} {mandatory_str}
                """)
            except Exception:
                pass  # Property may already exist or not apply to this edge type
    
    print("✓ Properties created")


def create_indexes():
    """Create indexes for fast lookups and traversals."""
    print("Creating indexes...")
    
    # Symbol indexes
    try:
        execute_command("CREATE INDEX Symbol.qualified_name ON Symbol (qualified_name) UNIQUE")
    except Exception:
        pass
    
    try:
        execute_command("CREATE INDEX Symbol.kind ON Symbol (kind) NOTUNIQUE")
    except Exception:
        pass
    
    try:
        execute_command("CREATE INDEX Symbol.file_path ON Symbol (file_path) NOTUNIQUE")
    except Exception:
        pass
    
    # Decision indexes
    try:
        execute_command("CREATE INDEX Decision.slug ON Decision (slug) UNIQUE")
    except Exception:
        pass
    
    # Lesson indexes
    try:
        execute_command("CREATE INDEX Lesson.slug ON Lesson (slug) UNIQUE")
    except Exception:
        pass
    
    # Edge indexes for fast traversal
    edge_classes = ["Calls", "Imports", "Inherits", "Governs", "Requires", "Touches"]
    for edge_class in edge_classes:
        try:
            execute_command(f"CREATE INDEX {edge_class}.out ON {edge_class} (out) NOTUNIQUE")
        except Exception:
            pass
        try:
            execute_command(f"CREATE INDEX {edge_class}.in ON {edge_class} (in) NOTUNIQUE")
        except Exception:
            pass
    
    print("✓ Indexes created")


def main():
    """Main entry point."""
    print(f"Setting up OrientDB schema for database: {DATABASE}")
    print(f"OrientDB URL: {ORIENTDB_URL}")
    print()
    
    try:
        create_vertex_classes()
        create_edge_classes()
        create_properties()
        create_indexes()
        
        print()
        print("=" * 60)
        print("Schema setup complete!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
