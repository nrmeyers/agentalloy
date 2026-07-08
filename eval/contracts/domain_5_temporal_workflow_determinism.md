---
phase: qa
task_slug: domain_5_temporal_workflow_determinism
domain_tags: [temporal, workflow]
---

A colleague wrote a Temporal workflow in Python that calls `datetime.now()` inside the workflow function to timestamp an event, and uses `random.uuid4()` to generate a correlation ID. Explain why this is problematic and how to fix it.
