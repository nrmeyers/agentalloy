#!/usr/bin/env python3
"""
Create a test DuckDB with sample symbols and edges for migration testing.
"""

import duckdb
from pathlib import Path

TEST_DB_PATH = ".agentalloy/test-repo/graph.duck"


def create_test_db():
    """Create a test DuckDB with sample code intelligence data."""
    print("Creating test DuckDB...")
    
    # Ensure directory exists
    db_path = Path(TEST_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Remove existing test DB
    if db_path.exists():
        db_path.unlink()
    
    # Connect and create schema
    conn = duckdb.connect(str(db_path))
    
    # Create symbols table
    conn.execute("""
        CREATE TABLE symbols (
            qualified_name VARCHAR PRIMARY KEY,
            kind VARCHAR,
            name VARCHAR,
            file_path VARCHAR,
            start_line INTEGER,
            end_line INTEGER,
            docstring VARCHAR,
            decorators VARCHAR,
            is_exported BOOLEAN,
            is_async BOOLEAN,
            is_generator BOOLEAN,
            source_code VARCHAR,
            contextual_prefix VARCHAR,
            content_hash VARCHAR,
            pagerank FLOAT
        )
    """)
    
    # Create edges table
    conn.execute("""
        CREATE TABLE edges (
            src VARCHAR,
            dst VARCHAR,
            kind VARCHAR,
            confidence FLOAT,
            resolved_via VARCHAR,
            file_path VARCHAR,
            line_start INTEGER
        )
    """)
    
    # Create centrality table
    conn.execute("""
        CREATE TABLE centrality (
            qualified_name VARCHAR PRIMARY KEY,
            pagerank FLOAT
        )
    """)
    
    # Insert sample symbols
    symbols = [
        ("auth.middleware.authenticate", "function", "authenticate", "src/auth/middleware.py",
         10, 25, "Authenticate request", None, True, False, False,
         "def authenticate(request): ...", "auth.middleware", "abc123", 0.15),
        
        ("auth.middleware.validate_token", "function", "validate_token", "src/auth/middleware.py",
         30, 45, "Validate JWT token", None, True, False, False,
         "def validate_token(token): ...", "auth.middleware", "def456", 0.12),
        
        ("auth.models.User", "class", "User", "src/auth/models.py",
         5, 50, "User model", None, True, False, False,
         "class User: ...", "auth.models", "ghi789", 0.18),
        
        ("api.routes.login", "function", "login", "src/api/routes.py",
         15, 30, "Login endpoint", ["@app.route"], True, True, False,
         "async def login(): ...", "api.routes", "jkl012", 0.10),
        
        ("api.routes.protected", "function", "protected", "src/api/routes.py",
         35, 50, "Protected endpoint", ["@app.route", "@auth_required"], True, False, False,
         "def protected(): ...", "api.routes", "mno345", 0.08),
        
        ("db.connection.get_connection", "function", "get_connection", "src/db/connection.py",
         10, 20, "Get DB connection", None, True, False, False,
         "def get_connection(): ...", "db.connection", "pqr678", 0.20),
        
        ("db.connection.close", "function", "close", "src/db/connection.py",
         25, 30, "Close connection", None, True, False, False,
         "def close(): ...", "db.connection", "stu901", 0.05),
    ]
    
    for sym in symbols:
        conn.execute("""
            INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, sym)
    
    # Insert sample edges
    edges = [
        # CALLS edges
        ("api.routes.login", "auth.middleware.validate_token", "CALLS", 1.0, "exact",
         "src/api/routes.py", 20),
        ("api.routes.protected", "auth.middleware.authenticate", "CALLS", 1.0, "exact",
         "src/api/routes.py", 40),
        ("auth.middleware.authenticate", "auth.middleware.validate_token", "CALLS", 1.0, "exact",
         "src/auth/middleware.py", 15),
        ("auth.middleware.validate_token", "db.connection.get_connection", "CALLS", 1.0, "exact",
         "src/auth/middleware.py", 35),
        
        # IMPORTS edges
        ("api.routes", "auth.middleware", "IMPORTS", 1.0, "exact",
         "src/api/routes.py", 1),
        ("auth.middleware", "auth.models", "IMPORTS", 1.0, "exact",
         "src/auth/middleware.py", 1),
        ("auth.middleware", "db.connection", "IMPORTS", 1.0, "exact",
         "src/auth/middleware.py", 2),
        
        # DEFINES edges
        ("auth.middleware", "auth.middleware.authenticate", "DEFINES", 1.0, "exact",
         "src/auth/middleware.py", 10),
        ("auth.middleware", "auth.middleware.validate_token", "DEFINES", 1.0, "exact",
         "src/auth/middleware.py", 30),
        ("auth.models", "auth.models.User", "DEFINES", 1.0, "exact",
         "src/auth/models.py", 5),
        ("api.routes", "api.routes.login", "DEFINES", 1.0, "exact",
         "src/api/routes.py", 15),
        ("api.routes", "api.routes.protected", "DEFINES", 1.0, "exact",
         "src/api/routes.py", 35),
        ("db.connection", "db.connection.get_connection", "DEFINES", 1.0, "exact",
         "src/db/connection.py", 10),
        ("db.connection", "db.connection.close", "DEFINES", 1.0, "exact",
         "src/db/connection.py", 25),
    ]
    
    for edge in edges:
        conn.execute("""
            INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?)
        """, edge)
    
    # Insert centrality data
    centrality = [
        ("auth.middleware.authenticate", 0.15),
        ("auth.middleware.validate_token", 0.12),
        ("auth.models.User", 0.18),
        ("api.routes.login", 0.10),
        ("api.routes.protected", 0.08),
        ("db.connection.get_connection", 0.20),
        ("db.connection.close", 0.05),
    ]
    
    for cent in centrality:
        conn.execute("""
            INSERT INTO centrality VALUES (?, ?)
        """, cent)
    
    conn.close()
    
    print(f"✓ Created test DuckDB at {TEST_DB_PATH}")
    print(f"  - {len(symbols)} symbols")
    print(f"  - {len(edges)} edges")
    print(f"  - {len(centrality)} centrality records")


if __name__ == "__main__":
    create_test_db()
