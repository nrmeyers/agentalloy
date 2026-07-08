---
phase: build
task_slug: domain_9_temporal_activity_timeouts
domain_tags: [temporal, activities]
---

You are writing a Temporal activity in Python that calls a slow third-party API which can take up to two minutes and occasionally hangs forever — and the only available client library is blocking (synchronous). Describe the timeout configuration you would set on the activity, how the workflow can tell that a long-running activity is still making progress, and how you would implement the activity given that the client library blocks.
