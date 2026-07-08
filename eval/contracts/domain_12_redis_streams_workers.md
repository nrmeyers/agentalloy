---
phase: build
task_slug: domain_12_redis_streams_workers
domain_tags: [redis, streams]
---

You need a durable work queue on Redis where multiple workers consume events in parallel, no event is lost if a worker crashes mid-processing, and stuck events eventually get reassigned to a healthy worker. Which Redis data structure and commands would you use? Walk through the lifecycle: producing an event, a worker claiming and finishing it, and recovering from a crashed worker.
