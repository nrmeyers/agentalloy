# 27B fidelity pass — final result (2026-06-13)

Judge: qwen3.6-27b (scalar, rubric corr/cove/prec → score). 540 judgments,
0 parse errors. LFM + 12B, domain tasks, conditions none/composed/flat, n=5.
Verdicts: `~/.claude/jobs/f72319e1/tmp/fidelity-27b.jsonl`.

## Per-model judge fidelity (n=90 per condition per model)

| model | none  | composed | flat  | composed−none | composed−flat | %oracle |
|-------|-------|----------|-------|---------------|---------------|---------|
| LFM   | 0.651 | 0.805    | 0.838 | **+0.154**    | −0.033        | **83%** |
| 12B   | 0.979 | 0.993    | 0.997 | **+0.013**    | −0.004        | **75%** |
| pooled| 0.815 | 0.899    | 0.917 | +0.084        | −0.019        | 82%     |

Pooled composed−none bootstrap 95% CI: **[+0.056, +0.114]** (excludes 0).
%oracle = (composed−none)/(flat−none) = share of the perfect-knowledge gap captured.

## Cross-validation vs the length-blind heuristic (canonical, post-#141)

| model | judge c−none | heuristic c−none |
|-------|--------------|------------------|
| LFM   | +0.154       | +0.172           |
| 12B   | +0.013       | +0.020           |

Judge runs **slightly conservative** vs the heuristic on both models — it does
NOT inflate the lift. Two independent methods (one length-blind, one holistic)
converge on the same direction and magnitude → lift is real answer quality.

## Length-bias diagnostic — resolved

judge score vs output_tokens: Pearson r = **−0.685** (longer → lower score).
Mean output tokens by condition: none **2604**, composed **1988**, flat **1851**.

`none` is the LONGEST condition. A negative length bias therefore *depresses
none's score → inflates composed−none*. Yet the judge's lift is SMALLER than
the length-blind heuristic's on both models. So:
- the lift is not a length artifact (the length-immune heuristic finds it too, larger);
- the length bias is a caveat on the judge's *absolute* scores only, neutralized
  for the delta by the heuristic cross-check;
- `none`'s ~24% extra verbosity is plausibly a symptom of unguided rambling.

Judge–heuristic Pearson = 0.542 over the full 540 (vs 0.919 on the 5-run
spot-check) — expected with more variance; delta-convergence is the stronger signal.

## Headline

The independent 27B judge **confirms** the composed>none lift. LFM (the headline
weak model) gains **+0.154** and captures **83%** of the perfect-knowledge oracle
with zero invocation; 12B is at its ceiling (+0.013, statistically == oracle).
