"""Tool definitions for the interpreter — 14 tools (11 read + 3 state).

MCP-standard JSON-Schema inputSchemas, ported from spike's validated ARG_SCHEMAS.
"""

# 11 READ tools
CODE_SEARCH = {
    "type": "function",
    "function": {
        "name": "code_search",
        "description": "Semantic code search. Returns relevant code snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text"},
                "k": {
                    "type": "integer",
                    "description": "Number of results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
}

SYMBOLS = {
    "type": "function",
    "function": {
        "name": "symbols",
        "description": "Look up a symbol by fully-qualified name.",
        "parameters": {
            "type": "object",
            "properties": {
                "fqn": {"type": "string", "description": "Fully-qualified symbol name"},
            },
            "required": ["fqn"],
        },
    },
}

GRAPH_QUERY = {
    "type": "function",
    "function": {
        "name": "graph_query",
        "description": (
            "Explore the code graph. With a query: seeds via hybrid search "
            "expanded `hops` deep. Empty query: top-centrality overview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for seed symbols; omit for an overview",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max nodes (default 20)",
                    "default": 20,
                },
                "hops": {
                    "type": "integer",
                    "description": "Graph radius, 1-3 (default 1)",
                    "default": 1,
                },
                "repo": {"type": "string", "description": "Restrict to one repo name"},
            },
            "required": [],
        },
    },
}

KNOWLEDGE_WHY = {
    "type": "function",
    "function": {
        "name": "knowledge_why",
        "description": "Read the design decision governing a specific symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "fqn": {"type": "string", "description": "Fully-qualified symbol name"},
            },
            "required": ["fqn"],
        },
    },
}

KNOWLEDGE_RELATED = {
    "type": "function",
    "function": {
        "name": "knowledge_related",
        "description": "Find design decisions related to a topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Topic text"},
                "k": {
                    "type": "integer",
                    "description": "Number of results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
}

KNOWLEDGE_ENTITIES = {
    "type": "function",
    "function": {
        "name": "knowledge_entities",
        "description": "List typed entity edges touching a symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "fqn": {
                    "type": "string",
                    "description": "Fully-qualified symbol name or short name",
                },
                "kind": {"type": "string", "description": "Edge kind filter"},
            },
            "required": ["fqn"],
        },
    },
}

ARTIFACT_BODY = {
    "type": "function",
    "function": {
        "name": "artifact_body",
        "description": "Read the full body of a recorded phase artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "Phase (spec|design|plan|build|qa|ship)",
                },
                "name": {"type": "string", "description": "Artifact name"},
            },
            "required": ["phase", "name"],
        },
    },
}

CONTRACT_DETAIL = {
    "type": "function",
    "function": {
        "name": "contract_detail",
        "description": "Read contract details by slug.",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Contract slug"},
            },
            "required": ["slug"],
        },
    },
}

TELEMETRY = {
    "type": "function",
    "function": {
        "name": "telemetry",
        "description": "Get recent interpreter traces (tool calls, stop reasons, phases).",
        "parameters": {
            "type": "object",
            "properties": {
                "k": {
                    "type": "integer",
                    "description": "Number of traces (default 5)",
                    "default": 5,
                },
                "phase": {"type": "string", "description": "Phase filter"},
            },
            "required": [],
        },
    },
}

GET_SKILL_FOR = {
    "type": "function",
    "function": {
        "name": "get_skill_for",
        "description": (
            "Skill selection. Without 'packs': returns the pack catalog "
            "(one row per pack). With 'packs': returns skill rows for those "
            "packs, each with a fragment-type breakdown. Pick 1-4 relevant "
            "packs first, then 2-6 skills."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description"},
                "phase": {"type": "string", "description": "Current phase"},
                "packs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pack names; omit for the pack catalog",
                },
            },
            "required": ["task", "phase"],
        },
    },
}

ASSEMBLE_SKILL = {
    "type": "function",
    "function": {
        "name": "assemble_skill",
        "description": (
            "Assemble the dynamic skill from the selected skills + fragment "
            "types. Deterministic; the assembled skill is injected into the "
            "main model's context. Call once, then write the brief."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill ids (2-6), most relevant first",
                },
                "types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "execution",
                            "example",
                            "rationale",
                            "verification",
                            "guardrail",
                            "setup",
                        ],
                    },
                    "description": "Fragment types to include; omit for all",
                },
                "phase": {
                    "type": "string",
                    "description": "Current phase (drops out-of-phase skills)",
                },
            },
            "required": ["skills"],
        },
    },
}

# 3 STATE tools
CONTRACT_ADD = {
    "type": "function",
    "function": {
        "name": "contract_add",
        "description": "Add or update a contract.",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Contract slug"},
                "domain_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Domain tags (max 2)",
                },
                "touches": {"type": "string", "description": "What this contract touches"},
            },
            "required": ["slug", "domain_tags", "touches"],
        },
    },
}

ARTIFACT_RECORD = {
    "type": "function",
    "function": {
        "name": "artifact_record",
        "description": "Record a phase artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "phase": {"type": "string", "description": "Phase"},
                "name": {"type": "string", "description": "Artifact name"},
                "body": {"type": "string", "description": "Artifact body"},
            },
            "required": ["phase", "name", "body"],
        },
    },
}

PHASE_ADVANCE = {
    "type": "function",
    "function": {
        "name": "phase_advance",
        "description": "Advance to the next phase.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target phase"},
                "approved": {"type": "boolean", "description": "Whether advance is approved"},
            },
            "required": ["target", "approved"],
        },
    },
}

# All 14 tools
ALL_TOOLS = [
    CODE_SEARCH,
    SYMBOLS,
    GRAPH_QUERY,
    KNOWLEDGE_WHY,
    KNOWLEDGE_RELATED,
    KNOWLEDGE_ENTITIES,
    ARTIFACT_BODY,
    CONTRACT_DETAIL,
    TELEMETRY,
    GET_SKILL_FOR,
    ASSEMBLE_SKILL,
    CONTRACT_ADD,
    ARTIFACT_RECORD,
    PHASE_ADVANCE,
]

READ_TOOLS = ALL_TOOLS[:11]
STATE_TOOLS = ALL_TOOLS[11:]
