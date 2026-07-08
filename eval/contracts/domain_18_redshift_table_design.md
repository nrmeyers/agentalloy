---
phase: design
task_slug: domain_18_redshift_table_design
domain_tags: [redshift, table design]
---

You are designing a star schema in Amazon Redshift: a 2-billion-row fact table joined to a large customer dimension, several small lookup dimensions, and queries that almost always filter on a date range. Explain your distribution strategy for the fact table, the large dimension, and the small lookups, your sort strategy, and the storage mechanism that makes range filters on the sort column fast.
