---
phase: design
task_slug: domain_4_webhook_versioning
domain_tags: [webhooks, versioning]
---

You need to make a breaking change to your webhook payload schema: you want to rename the field `customer_id` to `account_id`. Walk through the versioning strategy you would follow so that existing consumers are not broken. Include how you communicate the change and how long you maintain backward compatibility.
