# Vendored subset of codebase_rag/cypher_queries.py — only the incremental
# module-removal queries used by GraphUpdater.remove_file_from_state(). They
# are only executed when the ingestor implements execute_write() (a real
# graph-DB ingestor); collecting/stub ingestors skip this path entirely.

# Step 1 of 3: delete Methods of Classes defined by the module.
CYPHER_DELETE_MODULE_METHODS = """
MATCH (m:Module {qualified_name: $qn})-[:DEFINES]->(c:Class)-[:DEFINES_METHOD]->(meth:Method)
DETACH DELETE meth
"""

# Step 2 of 3: delete Functions, Classes, Interfaces, and Enums directly defined
# by the module (DETACH DELETE removes their outgoing relationships automatically).
CYPHER_DELETE_MODULE_DEFINES = """
MATCH (m:Module {qualified_name: $qn})-[:DEFINES]->(node)
DETACH DELETE node
"""

# Step 3 of 3: delete the Module node itself (DETACH DELETE removes remaining
# CONTAINS_MODULE, IMPORTS, CALLS, BELONGS_TO edges on the module).
CYPHER_DELETE_MODULE_NODE = """
MATCH (m:Module {qualified_name: $qn})
DETACH DELETE m
"""

# Remove Package nodes that no longer contain any Module or sub-Package children.
# Runs after module deletion so stale parent packages are cleaned up.
CYPHER_DELETE_ORPHAN_PACKAGES = """
MATCH (pkg:Package)
WHERE NOT (pkg)-[:CONTAINS_MODULE]->(:Module)
AND NOT (pkg)-[:CONTAINS_PACKAGE]->(:Package)
DETACH DELETE pkg
"""
