---
phase: build
task_slug: domain_17_airflow_task_hygiene
domain_tags: [airflow, best-practices]
---

An Airflow DAG has three problems: one task produces a 2 GB dataframe that the next task needs; several tasks embed database passwords directly in the code; and re-running a task after a failure double-inserts rows into the target table. Describe the best-practice fix for each problem.
