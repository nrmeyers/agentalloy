---
phase: build
task_slug: domain_7_dbt_incremental
domain_tags: [dbt, models]
---

You have a dbt model over a 500 M-row events table. Full refreshes take 45 minutes. Explain how to convert this model to run incrementally. What macro gates the incremental filter, what configuration parameter prevents duplicate rows when records are updated, and what happens on the very first run?
