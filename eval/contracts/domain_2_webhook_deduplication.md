---
phase: build
task_slug: domain_2_webhook_deduplication
domain_tags: [idempotency, deduplication]
---

A webhook consumer sometimes receives the same event twice due to retries from the platform. Describe how you would implement exactly-once processing. Which header uniquely identifies a delivery attempt vs a message, and what storage pattern would you use to track seen events?
