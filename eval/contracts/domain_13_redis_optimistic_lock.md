---
phase: build
task_slug: domain_13_redis_optimistic_lock
domain_tags: [redis, transactions]
---

Implement a safe read-modify-write of an account balance stored in Redis, without using Lua scripts. Two clients may race on the same key. Describe the transaction mechanism you would use, how a racing write is detected, and what the losing client should do.
