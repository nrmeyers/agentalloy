---
phase: build
task_slug: domain_16_otel_trace_propagation
domain_tags: [distributed tracing, opentelemetry]
---

Service A calls service B over HTTP and you want a single distributed trace covering both services. Explain how spans created in service B end up in the same trace as service A's spans: what identifies the trace, how a span declares its parent, how that context crosses the HTTP boundary, and how you can tell which span is the root of the trace. Finally, how do you keep tracing costs bounded in high-traffic systems?
