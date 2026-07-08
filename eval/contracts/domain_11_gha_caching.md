---
phase: build
task_slug: domain_11_gha_caching
domain_tags: [caching, artifacts]
---

A Node.js CI workflow reinstalls all dependencies from scratch on every run. Explain how to add dependency caching with GitHub Actions: how the cache key should be constructed so the cache is invalidated when the lockfile changes, what fallback you would configure to still get partial reuse, and what happens on a cache miss.
