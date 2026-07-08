---
phase: build
task_slug: domain_14_snowflake_time_travel
domain_tags: [time-travel, streams]
---

An analyst ran a bad UPDATE that corrupted a Snowflake table an hour ago, and a staging table was accidentally dropped yesterday. Explain how to query the table's state from before the bad UPDATE, how to recover the dropped table, and what setting bounds how far back these operations can reach. Separately: how would you set up incremental downstream processing that consumes only the rows that changed in this table?
