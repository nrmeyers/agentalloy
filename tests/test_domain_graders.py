"""Regression tests for the 2026-06-13 domain-grader robustness audit.

Each loosened criterion gets two assertions: the paraphrase it newly accepts
(false-negative fixed) AND a vague/wrong answer it must still reject
(guards against loosening into a false-positive). Keeping both directions
pinned protects the benchmark's integrity if the keyword lists are edited later.
"""

from __future__ import annotations

from eval.domain_tasks import (
    grade_domain_1_webhook_signature,
    grade_domain_4_webhook_versioning,
    grade_domain_9_temporal_activity_timeouts,
    grade_domain_16_otel_trace_propagation,
)


def test_otel_parent_id_accepts_canonical_field_name() -> None:
    # parent_span_id is the canonical OTel field; "parent_id"/"parent span"
    # substrings do not appear inside it, so it used to false-negative.
    out = "Each span sets its parent_span_id to the span_id of its caller."
    assert grade_domain_16_otel_trace_propagation(out)["explains_parent_id"] is True


def test_otel_parent_id_still_rejects_unrelated_text() -> None:
    out = "Spans share a trace_id and you should sample to bound cost."
    assert grade_domain_16_otel_trace_propagation(out)["explains_parent_id"] is False


def test_signed_content_accepts_signing_string_phrasing() -> None:
    # "signing string"/"signing_input" built from timestamp+body is exactly the
    # composed signed-content construction under a different name.
    out = 'Build the signing string: signing_input = f"{timestamp}.{body}", then HMAC it.'
    assert grade_domain_1_webhook_signature(out)["mentions_signed_content_tuple"] is True


def test_signed_content_still_rejects_vague_payload_hmac() -> None:
    # A bare "HMAC of the payload" that never combines the timestamp must fail:
    # it does not describe the composed signed content (no "body" token + join).
    out = "Compute an HMAC-SHA256 of the payload and compare it to the header."
    assert grade_domain_1_webhook_signature(out)["mentions_signed_content_tuple"] is False


def test_version_header_accepts_alternate_header_names() -> None:
    out = "Emit a `webhook-schema-version` HTTP header on every delivery."
    assert grade_domain_4_webhook_versioning(out)["mentions_version_header"] is True


def test_version_header_rejects_payload_field_only() -> None:
    # Putting the version in a payload *field* (no header) is a different,
    # also-valid strategy that this specific criterion does not credit.
    out = 'Add a "schema_version" field to the JSON payload body.'
    assert grade_domain_4_webhook_versioning(out)["mentions_version_header"] is False


def test_activity_total_bound_accepts_overall_timeout_phrasing() -> None:
    out = "Also cap the total execution time across all retries so it cannot loop forever."
    assert (
        grade_domain_9_temporal_activity_timeouts(out)["mentions_schedule_to_close_or_total_bound"]
        is True
    )


def test_activity_total_bound_rejects_retry_policy_only() -> None:
    # Naming a retry policy without bounding total time is the per-attempt
    # concept (start_to_close), not the total bound this criterion rewards.
    out = "Set start_to_close_timeout and a RetryPolicy with maximum_attempts=3."
    assert (
        grade_domain_9_temporal_activity_timeouts(out)["mentions_schedule_to_close_or_total_bound"]
        is False
    )
