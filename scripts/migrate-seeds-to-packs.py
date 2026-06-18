#!/usr/bin/env python3
"""Reorganize seeds/ into seeds/packs/<name>/ with a pack.yaml per pack.

Reads every YAML under seeds/ (excluding seeds/packs/), classifies it by
skill_id against PACK_RULES, and either:
  - dry-run mode: prints (skill_id → pack) decisions and pack-level summary
  - apply mode:    git-mvs YAMLs into seeds/packs/<pack>/ and writes pack.yaml

Usage:
  python scripts/migrate-seeds-to-packs.py --dry-run
  python scripts/migrate-seeds-to-packs.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = REPO_ROOT / "seeds"
# Packs live INSIDE the package so they ship with the wheel.
PACKS_DIR = REPO_ROOT / "src" / "agentalloy" / "_packs"

# Order matters: first match wins.
# Strategy: framework-specific patterns first, then services, cross-cutting
# domains, then language catch-alls, then engineering catch-all, then core.
PACK_RULES: list[tuple[str, str]] = [
    # ── Frameworks (specific identifiers — match before language catch-alls) ──
    (r"^vue-.*", "vue"),
    (r"^next-.*|^nextjs-.*|^deploy-to-vercel$|^vercel-cli-with-tokens$", "nextjs"),
    (
        r"^vercel-react-(?!native).*|^vercel-composition-patterns$|^react-modernization$|^react-state-management$",
        "react",
    ),
    (r"^fastify-.*", "fastify"),
    (r"^fastapi-.*", "fastapi"),
    (r"^nestjs-.*", "nestjs"),
    (r"^temporal-.*", "temporal"),
    # ── Service integrations ──
    (r"^redis-.*", "redis"),
    (r"^s3-.*", "s3"),
    (r"^(postgresql|sql-optimization-patterns|database-migration)$", "postgres"),
    # ── Cross-cutting domains ──
    (r"^auth-.*|^msal-.*|^oauth-.*", "auth"),
    (
        r"^(prometheus-configuration|grafana-dashboards|distributed-tracing|"
        r"slo-implementation|service-mesh-observability|python-observability)$",
        "observability",
    ),
    (
        r"^(k8s-.*|helm-.*|istio-.*|linkerd-.*|mtls-configuration|"
        r"hybrid-cloud-networking|multi-cloud-architecture)",
        "containers",
    ),
    (r"^terraform-.*", "iac"),
    (
        r"^(github-actions-templates|gitlab-ci-patterns|gitops-workflow|"
        r"deployment-pipeline-design|ci-cd-and-automation)$",
        "cicd",
    ),
    (
        r"^(secrets-management|sast-configuration|stride-.*|attack-tree-.*|"
        r"threat-mitigation-.*|security-and-hardening|security-requirement-extraction|"
        r"memory-safety-patterns|bash-defensive-patterns|shellcheck-configuration)$",
        "security",
    ),
    (
        r"^(prompt-engineering-.*|rag-implementation|langchain-.*|llm-evaluation|"
        r"embedding-strategies|hybrid-search-.*|similarity-search-.*|"
        r"vector-index-.*|context-engineering|protect-mcp-setup|signed-audit-trails-.*)$",
        "agents",
    ),
    (
        r"^(javascript-testing-patterns|bats-testing-patterns|"
        r"e2e-testing-patterns|browser-testing-with-devtools)$",
        "testing",
    ),
    (r"^(eslint-.*|linting-.*)", "linting"),
    (
        r"^(mobile-.*|react-native-.*|vercel-react-native-.*|vercel-react-view-transitions|"
        r"tailwind-design-system|design-system-patterns|interaction-design|"
        r"responsive-design|visual-design-foundations|wcag-audit-patterns|"
        r"screen-reader-testing|web-component-design|web-design-guidelines|"
        r"frontend-ui-engineering)$",
        "ui-design",
    ),
    # ── Specialized ──
    (
        r"^(airflow-dag-patterns|dbt-transformation-patterns|spark-optimization|"
        r"ml-pipeline-workflow|data-quality-frameworks)$",
        "data-engineering",
    ),
    (
        r"^(bazel-build-optimization|monorepo-management|nx-workspace-patterns|turborepo-caching)$",
        "monorepo",
    ),
    # ── Languages (after framework + domain rules) ──
    (r"^node-.*|^nodejs-.*", "nodejs"),
    (r"^typescript-.*", "typescript"),
    (r"^python-.*|^uv-package-manager$|^async-python-patterns$", "python"),
    (r"^rust-.*", "rust"),
    (r"^go-.*", "go"),
    # ── Engineering: generic patterns (architecture, API, error, perf) ──
    (
        r"^(api-and-interface-design|api-design-principles|openapi-spec-generation|"
        r"architecture-decision-records|architecture-patterns|microservices-patterns|"
        r"cqrs-implementation|event-store-design|projection-patterns|saga-orchestration|"
        r"error-handling-patterns|performance-optimization|workflow-orchestration-patterns|"
        r"modern-javascript-patterns|documentation-and-adrs|evaluation-methodology|"
        r"cost-optimization)$",
        "engineering",
    ),
    # ── SDD pipeline workflow skills ──
    (r"^sdd-", "sdd"),
    # ── Everything else → core (process, governance, generic) ──
    (r".*", "core"),
]

PACK_METADATA = {
    "core": {
        "description": "Process & governance — TDD, code review, planning, debugging, git, brainstorming, verification, dispatching, dependency management. Always installed.",
        "always_install": True,
        "depends_on": [],
    },
    "engineering": {
        "description": "Generic engineering patterns — API design, error handling, ADRs, microservices, performance, CQRS, event sourcing. Always installed.",
        "always_install": True,
        "depends_on": [],
    },
    "nodejs": {
        "description": "Node.js (22+) — async, streams, errors, perf, profiling, type-stripping, V8/libuv internals.",
        "depends_on": [],
    },
    "typescript": {
        "description": "TypeScript advanced types — generics, conditionals, mapped types, brand types, error diagnosis.",
        "depends_on": [],
    },
    "python": {
        "description": "Python ecosystem — async, packaging, testing, error handling, observability, type safety.",
        "depends_on": [],
    },
    "rust": {
        "description": "Rust — async runtimes, ownership patterns, error handling.",
        "depends_on": [],
    },
    "go": {
        "description": "Go — concurrency patterns, error handling, idiomatic style.",
        "depends_on": [],
    },
    "nestjs": {
        "description": "NestJS framework — modules, providers, DI, ValidationPipe, guards, interceptors, exception filters.",
        "depends_on": ["nodejs", "typescript"],
    },
    "fastify": {
        "description": "Fastify framework — plugins, JSON Schema, hooks, pino logging, performance.",
        "depends_on": ["nodejs", "typescript"],
    },
    "react": {
        "description": "React (web) — modernization, state management, composition, view transitions.",
        "depends_on": ["typescript"],
    },
    "vue": {
        "description": "Vue 3 — Composition API, Pinia, Router, JSX, debugging gotchas, testing.",
        "depends_on": ["typescript"],
    },
    "nextjs": {
        "description": "Next.js — App Router, Cache Components (16+), upgrade workflows, Vercel deployment.",
        "depends_on": ["react"],
    },
    "fastapi": {
        "description": "FastAPI — project templates, dependency injection, async endpoints.",
        "depends_on": ["python"],
    },
    "temporal": {
        "description": "Temporal durable workflows — TypeScript SDK + Python testing strategies.",
        "depends_on": [],
    },
    "redis": {
        "description": "Redis runtime — caching, distributed locks, pub/sub, rate limiting.",
        "depends_on": [],
    },
    "s3": {
        "description": "AWS S3 — file storage, presigned URLs, multipart uploads, streaming.",
        "depends_on": [],
    },
    "postgres": {
        "description": "PostgreSQL — table design, query optimization, migrations.",
        "depends_on": [],
    },
    "auth": {
        "description": "Authentication & authorization — OAuth 2.0, MSAL Microsoft 365 SSO, JWT patterns.",
        "depends_on": [],
    },
    "observability": {
        "description": "Observability — Prometheus, Grafana, distributed tracing, SLO implementation, service mesh observability.",
        "depends_on": [],
    },
    "containers": {
        "description": "Container orchestration — Kubernetes, Helm, service mesh (Istio/Linkerd), mTLS, multi-cloud networking.",
        "depends_on": [],
    },
    "iac": {
        "description": "Infrastructure as Code — Terraform module library and patterns.",
        "depends_on": [],
    },
    "cicd": {
        "description": "CI/CD pipelines — GitHub Actions, GitLab CI, GitOps, deployment pipeline design.",
        "depends_on": [],
    },
    "security": {
        "description": "Security — secrets management, SAST, threat modeling (STRIDE/attack trees), hardening, defensive patterns.",
        "depends_on": [],
    },
    "agents": {
        "description": "Agentic development — prompt engineering, RAG, LangChain, MCP, LLM evaluation, vector search, audit trails.",
        "depends_on": [],
    },
    "testing": {
        "description": "Testing patterns — JavaScript testing, bats, e2e, browser testing with DevTools.",
        "depends_on": [],
    },
    "linting": {
        "description": "Linting — ESLint flat config, neostandard, migration from legacy ESLint.",
        "depends_on": ["typescript"],
    },
    "ui-design": {
        "description": "UI/UX — mobile (iOS/Android), React Native, web design, accessibility, Tailwind, design systems.",
        "depends_on": [],
    },
    "data-engineering": {
        "description": "Data engineering — Airflow, dbt, Spark, ML pipelines, data quality frameworks.",
        "depends_on": [],
    },
    "monorepo": {
        "description": "Monorepo build tooling — Bazel, Nx, Turborepo, monorepo management patterns.",
        "depends_on": [],
    },
    "mongodb": {
        "description": "MongoDB — document modeling, aggregation pipelines, indexing, transactions.",
        "depends_on": [],
    },
    "prisma": {
        "description": "Prisma ORM — schema design, migrations, query API, type safety.",
        "depends_on": [],
    },
    "graphql": {
        "description": "GraphQL — schema design, resolvers, subscriptions, federation.",
        "depends_on": [],
    },
    "webhooks": {
        "description": "Webhook design — delivery guarantees, retries, signature verification, idempotency.",
        "depends_on": [],
    },
    "websockets": {
        "description": "WebSocket scaling — connection management, backpressure, pub/sub patterns.",
        "depends_on": [],
    },
    "mocha-chai": {
        "description": "Mocha + Chai testing — test structure, assertions, async patterns.",
        "depends_on": [],
    },
    "vite": {
        "description": "Vite — bundling, plugins, dev server, build optimisation.",
        "depends_on": [],
    },
    "sdd": {
        "description": "SDD pipeline workflow skills (spec → design → plan → testgen → build → verify → deliver)",
        "depends_on": [],
        "always_install": False,
    },
}

PACK_VERSION = "1.0.0"
EMBED_MODEL = "nomic-embed-text-v1.5"
EMBED_DIM = 768

# Pack tier — see install_pack._VALID_PACK_TIERS and docs/PACK-AUTHORING.md.
PACK_TIERS: dict[str, str] = {
    "core": "foundation",
    "engineering": "foundation",
    "nodejs": "language",
    "typescript": "language",
    "python": "language",
    "rust": "language",
    "go": "language",
    "nestjs": "framework",
    "fastify": "framework",
    "react": "framework",
    "vue": "framework",
    "nextjs": "framework",
    "fastapi": "framework",
    "postgres": "store",
    "mongodb": "store",
    "redis": "store",
    "s3": "store",
    "temporal": "store",
    "auth": "cross-cutting",
    "security": "cross-cutting",
    "observability": "cross-cutting",
    "containers": "platform",
    "iac": "platform",
    "cicd": "platform",
    "monorepo": "platform",
    "testing": "tooling",
    "linting": "tooling",
    "mocha-chai": "tooling",
    "vite": "tooling",
    "agents": "domain",
    "ui-design": "domain",
    "data-engineering": "domain",
    "prisma": "store",
    "graphql": "protocol",
    "webhooks": "protocol",
    "websockets": "protocol",
    "sdd": "workflow",
}


def classify(skill_id: str) -> str:
    for pattern, pack in PACK_RULES:
        if re.match(pattern, skill_id):
            return pack
    return "core"


def collect_yamls() -> list[Path]:
    """Find every YAML in seeds/ except those already under seeds/packs/."""
    out: list[Path] = []
    for path in SEEDS.rglob("*.yaml"):
        try:
            path.relative_to(PACKS_DIR)
            continue  # already in packs/
        except ValueError:
            pass
        out.append(path)
    return sorted(out)


def load_skill(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_pack_manifest(pack: str, entries: list[dict]) -> None:
    meta = PACK_METADATA.get(pack, {"description": f"{pack} pack.", "depends_on": []})
    tier = PACK_TIERS.get(pack)
    if tier is None:
        raise ValueError(
            f"pack '{pack}' has no entry in PACK_TIERS — add one in "
            f"scripts/migrate-seeds-to-packs.py before regenerating manifests"
        )
    manifest = {
        "name": pack,
        "version": PACK_VERSION,
        "tier": tier,
        "description": meta["description"],
        "author": "navistone",
        "embed_model": EMBED_MODEL,
        "embedding_dim": EMBED_DIM,
        "license": "MIT",
        "homepage": "https://github.com/nrmeyers/agentalloy",
        "always_install": meta.get("always_install", False),
        "depends_on": meta.get("depends_on", []),
        "skills": entries,
    }
    out = PACKS_DIR / pack / "pack.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")


def git_mv(src: Path, dst: Path, *, use_git: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if use_git:
        try:
            subprocess.run(
                ["git", "mv", "-f", str(src), str(dst)],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            return
        except subprocess.CalledProcessError:
            pass  # fall through to plain move (e.g., file not tracked)
    shutil.move(str(src), str(dst))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Print decisions, don't move files")
    g.add_argument("--apply", action="store_true", help="Actually move files and write manifests")
    p.add_argument("--no-git", action="store_true", help="Use plain mv instead of git mv")
    args = p.parse_args(argv)

    yamls = collect_yamls()
    if not yamls:
        print("No YAMLs found outside seeds/packs/. Nothing to do.")
        return 0

    decisions: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    skipped: list[tuple[Path, str]] = []

    for path in yamls:
        try:
            data = load_skill(path)
        except yaml.YAMLError as exc:
            skipped.append((path, f"yaml-error: {exc}"))
            continue
        sid = data.get("skill_id")
        if not sid:
            skipped.append((path, "missing skill_id"))
            continue
        pack = classify(str(sid))
        decisions[pack].append((path, data))

    print(f"\n=== Classification ({len(yamls)} YAMLs → {len(decisions)} packs) ===\n")
    for pack in sorted(decisions, key=lambda p: (-len(decisions[p]), p)):
        entries = decisions[pack]
        meta = PACK_METADATA.get(pack, {})
        marker = " (always)" if meta.get("always_install") else ""
        deps = meta.get("depends_on", [])
        dep_str = f" deps={deps}" if deps else ""
        print(f"  {pack:18}{marker:9} {len(entries):4} skills{dep_str}")
    if skipped:
        print(f"\n  SKIPPED: {len(skipped)}")
        for path, reason in skipped[:10]:
            print(f"    {path.relative_to(REPO_ROOT)}: {reason}")

    if args.dry_run:
        print("\n=== Per-skill decisions ===")
        for pack in sorted(decisions):
            print(f"\n[{pack}]")
            for path, data in decisions[pack]:
                print(f"  {data['skill_id']:60} ← {path.relative_to(REPO_ROOT)}")
        return 0

    print("\n=== Applying ===")
    for pack in sorted(decisions):
        pack_dir = PACKS_DIR / pack
        pack_dir.mkdir(parents=True, exist_ok=True)
        manifest_skills: list[dict] = []

        for src, data in decisions[pack]:
            dst = pack_dir / src.name
            if dst.exists() and dst.resolve() != src.resolve():
                # Collision — append parent name as prefix
                dst = pack_dir / f"{src.parent.name}__{src.name}"
            git_mv(src, dst, use_git=not args.no_git)
            frag_count = len(data.get("fragments") or [])
            manifest_skills.append(
                {
                    "skill_id": data["skill_id"],
                    "file": dst.name,
                    "fragment_count": frag_count,
                }
            )

        manifest_skills.sort(key=lambda s: s["skill_id"])
        write_pack_manifest(pack, manifest_skills)
        print(f"  {pack}: {len(manifest_skills)} skills + pack.yaml")

    # Cleanup empty source dirs (anything in seeds/ that isn't packs/)
    for sub in SEEDS.iterdir():
        if sub.is_dir() and sub != PACKS_DIR:
            try:
                remaining = [r for r in sub.rglob("*") if r.is_file()]
                if not remaining:
                    shutil.rmtree(sub)
                    print(f"  cleaned empty dir: {sub.relative_to(REPO_ROOT)}")
            except OSError as e:
                print(f"  warn: could not clean {sub}: {e}")

    print("\nDone. Review with: git status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
