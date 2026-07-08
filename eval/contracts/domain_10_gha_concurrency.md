---
phase: build
task_slug: domain_10_gha_concurrency
domain_tags: [github-actions, concurrency]
---

Your CI workflow runs on every push to a PR branch, and your deploy workflow pushes to production. Rapid successive pushes waste compute on stale CI runs, and two deploys must never run at the same time. Show how you would configure this in GitHub Actions workflow YAML, and explain why the two workflows need different settings.
