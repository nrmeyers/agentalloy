#!/usr/bin/env python3
"""
Set up NebulaGraph schema for AgentAlloy code intelligence.

Creates tags (vertex types) and edge types for the code graph.
"""

from nebula3.gclient.net import ConnectionPool
from nebula3.Config import Config
import sys

NEBULA_HOST = "127.0.0.1"
NEBULA_PORT = 9669
NEBULA_USER = "root"
NEBULA_PASSWORD = "nebula"
SPACE_NAME = "agentalloy"


def execute(client, sql: str, description: str = ""):
    """Execute a NebulaGraph statement."""
    try:
        result = client.execute(sql)
        if result.is_succeeded():
            if description:
                print(f"  ✓ {description}")
            return result
        else:
            print(f"  ✗ {description or sql[:50]}")
            print(f"    Error: {result.error_msg()}")
            return None
    except Exception as e:
        print(f"  ✗ {description or sql[:50]}")
        print(f"    Exception: {e}")
        return None


def create_space(client):
    """Create the agentalloy space."""
    print("\n=== Creating Space ===")
    
    # Create space with FIXED_STRING vertex IDs (for qualified names)
    execute(
        client,
        f"CREATE SPACE IF NOT EXISTS {SPACE_NAME} (partition_num=10, replica_factor=1, vid_type=FIXED_STRING(256))",
        "Created agentalloy space"
    )
    
    # Wait for space to be ready (NebulaGraph needs time to propagate)
    import time
    print("  Waiting for space to be ready...")
    time.sleep(5)
    
    # Use the space
    result = execute(client, f"USE {SPACE_NAME}", "Using agentalloy space")
    if not result:
        print("  ✗ Failed to use space, exiting")
        sys.exit(1)


def create_tags(client):
    """Create vertex tags (Symbol, Decision, Lesson)."""
    print("\n=== Creating Tags ===")
    
    # Symbol tag - represents code symbols (functions, classes, modules, etc.)
    execute(
        client,
        """
        CREATE TAG IF NOT EXISTS Symbol (
            kind STRING,
            name STRING,
            file_path STRING,
            start_line INT,
            end_line INT,
            docstring STRING,
            decorators STRING,
            is_exported BOOL,
            is_async BOOL,
            is_generator BOOL,
            source_code STRING,
            contextual_prefix STRING,
            content_hash STRING,
            pagerank DOUBLE
        )
        """,
        "Created Symbol tag"
    )
    
    # Decision tag - represents design decisions
    execute(
        client,
        """
        CREATE TAG IF NOT EXISTS Decision (
            slug STRING,
            title STRING,
            body STRING,
            source_path STRING,
            created_at STRING,
            updated_at STRING,
            metadata STRING
        )
        """,
        "Created Decision tag"
    )
    
    # Lesson tag - represents lessons learned
    execute(
        client,
        """
        CREATE TAG IF NOT EXISTS Lesson (
            slug STRING,
            title STRING,
            body STRING,
            source_path STRING,
            promoted BOOL,
            created_at STRING
        )
        """,
        "Created Lesson tag"
    )
    
    # Meta tag - key-value metadata
    execute(
        client,
        """
        CREATE TAG IF NOT EXISTS Meta (
            meta_key STRING,
            meta_value STRING,
            updated_at INT
        )
        """,
        "Created Meta tag"
    )


def create_edge_types(client):
    """Create edge types for code and knowledge relationships."""
    print("\n=== Creating Edge Types ===")
    
    # Code edges
    code_edges = [
        ("Calls", "confidence DOUBLE, resolved_via STRING, file_path STRING, line_start INT"),
        ("Imports", "confidence DOUBLE, resolved_via STRING"),
        ("Inherits", "confidence DOUBLE"),
        ("Implements", "confidence DOUBLE"),
        ("Overrides", "confidence DOUBLE"),
        ("Defines", "confidence DOUBLE"),
        ("HasMember", "confidence DOUBLE"),
    ]
    
    for edge_name, props in code_edges:
        execute(
            client,
            f"CREATE EDGE {edge_name} ({props})",
            f"Created {edge_name} edge type"
        )
    
    # Knowledge edges
    knowledge_edges = [
        ("Governs", "resolution_tier STRING"),
        ("Requires", ""),
        ("Touches", ""),
        ("Constraints", ""),
        ("Command", ""),
        ("Stakeholder", ""),
    ]
    
    for edge_name, props in knowledge_edges:
        props_clause = f"({props})" if props else "()"
        execute(
            client,
            f"CREATE EDGE {edge_name} {props_clause}",
            f"Created {edge_name} edge type"
        )


def create_indexes(client):
    """Create indexes for fast lookups."""
    print("\n=== Creating Indexes ===")
    
    # Symbol indexes
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_symbol_name ON Symbol(name(64))",
        "Created Symbol name index"
    )
    
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_symbol_kind ON Symbol(kind(32))",
        "Created Symbol kind index"
    )
    
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_symbol_file ON Symbol(file_path(128))",
        "Created Symbol file_path index"
    )
    
    # Decision indexes
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_decision_slug ON Decision(slug(64))",
        "Created Decision slug index"
    )
    
    # Lesson indexes
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_lesson_slug ON Lesson(slug(64))",
        "Created Lesson slug index"
    )
    
    # Meta index
    execute(
        client,
        "CREATE TAG INDEX IF NOT EXISTS idx_meta_key ON Meta(meta_key(64))",
        "Created Meta key index"
    )


def main():
    """Main entry point."""
    print("=" * 60)
    print("NebulaGraph Schema Setup for AgentAlloy")
    print("=" * 60)
    print(f"\nConnecting to {NEBULA_HOST}:{NEBULA_PORT}...")
    
    # Create connection pool
    config = Config()
    config.max_connection_pool_size = 10
    
    pool = ConnectionPool()
    if not pool.init([(NEBULA_HOST, NEBULA_PORT)], config):
        print(f"\n✗ Failed to connect to NebulaGraph at {NEBULA_HOST}:{NEBULA_PORT}")
        sys.exit(1)
    
    print("✓ Connected")
    
    # Get session
    client = pool.get_session(NEBULA_USER, NEBULA_PASSWORD)
    
    try:
        create_space(client)
        create_tags(client)
        create_edge_types(client)
        create_indexes(client)
        
        # Rebuild indexes (required after creation)
        print("\n=== Rebuilding Indexes ===")
        execute(client, "REBUILD TAG INDEX", "Rebuilding all tag indexes")
        
        print("\n" + "=" * 60)
        print("Schema setup complete!")
        print("=" * 60)
        
    finally:
        client.release()
        pool.close()


if __name__ == "__main__":
    main()
