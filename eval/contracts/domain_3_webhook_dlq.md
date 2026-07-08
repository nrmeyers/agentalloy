---
phase: design
task_slug: domain_3_webhook_dlq
domain_tags: [webhooks, replay]
---

Design a webhook delivery system that handles endpoint failures gracefully. Describe the retry schedule you would use, when a delivery should be considered permanently failed and moved to a dead-letter queue, and what data each DLQ entry must contain to support triage and later replay.
