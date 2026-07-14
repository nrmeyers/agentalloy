# Skill Output Formatting

**skill_id:** sys-skill-output-formatting
**category:** tooling
**always_apply:** false
**phase_scope:**
**category_scope:** tooling
**author:** navistone
**change_summary:** initial authoring 2026-05-04 — conventions pack, codifies markdown structure, code-fence rules, table conventions, and citation format used across skill source markdown.

Skill source markdown is consumed by two readers: the bootstrap parser (which preserves it as `raw_prose`) and the embedder (which indexes it). Format choices that confuse either reader cost retrieval quality. This skill defines the formatting conventions that survive both consumers.

## Headings

Use ATX-style headings (`#`, `##`, `###`). Never use Setext (underline-style) headings — the bootstrap parser handles both, but `##` is unambiguous and survives copy-paste. The first H1 is the canonical name. Use `##` for top-level sections, `###` sparingly for subsections within a long fragment. Do not skip levels (no jumping `#` → `###`).

Headings double as fragment-boundary candidates during the source-to-YAML transform. Place a `##` at every natural fragment break — the transform agent uses headings to seed the fragment slice.

## Code fences

Always label code fences with the language: ` ```python `, ` ```typescript `, ` ```sql `, ` ```bash `. Unlabelled fences fall through to plain-text rendering and the embedder treats them as prose, which dilutes the surrounding fragment's vector. For shell sessions, use ` ```bash ` not ` ```sh ` — the embedder has stronger token coverage on `bash`.

For inline code, use single backticks for symbols (`hmac.compare_digest`), file paths (`src/agentalloy/_packs/`), env vars (`STRIPE_WEBHOOK_SECRET`), and HTTP headers (`X-Hub-Signature-256`). Do not backtick prose.

## Tables

Use GitHub-flavored markdown tables with explicit column alignment only when alignment carries meaning. Keep tables narrow — over six columns becomes unreadable in retrieval previews. Tables are usually `rationale` fragments, not `execution`.

## Lists

Hyphen bullets (`-`), not asterisks. Bullet items should be one sentence or one short paragraph; longer items become numbered steps or prose. Mixing nested bullets two levels deep is fine; three levels is a signal to refactor into prose or sub-headings.

## Citations

Cite primary sources inline with a verification date and the URL: `(verified 2026-05-04, https://stripe.com/docs/webhooks#retries)`. The verification date is non-negotiable per R5 — readers six months out need to know whether to trust or re-check. URL last so the prose reads cleanly when the citation is stripped.

For multi-source claims, list the sources at the end of the relevant fragment as a `Sources` subsection rather than peppering inline:

```
Sources (all verified 2026-05-04):
- Stripe webhook signatures: https://docs.stripe.com/webhooks/signatures
- GitHub webhook validation: https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
```

A trailing `## Verified` block at the end of `raw_prose` lists the verification anchors for the whole skill — used when many fragments share a citation set.

## Emphasis

Use `**bold**` for terms being defined ("HMAC-SHA256 is the field consensus signing primitive"). Use `*italic*` rarely — only for terms-of-art being introduced. Avoid all-caps for emphasis; the embedder treats casing as a signal and ALL-CAPS shifts vectors unpredictably.

## File-path-and-line references

When pointing at a specific line, use the `path:line` convention: `src/agentalloy/bootstrap.py:152`. The CLI reader can navigate to the line directly. Do not write "line 152 of bootstrap.py" — the colon form is queryable.

## Verbatim blocks

For verbatim shell sessions or output, use a fenced code block with no language label is acceptable IF the content is genuinely unstructured output:

```
$ git status
On branch main
nothing to commit, working tree clean
```

Otherwise label with the closest language (`bash`, `text`).

## Section ordering within `raw_prose`

The conventional ordering for a domain skill source: rationale → setup → execution → example → verification → guardrail. The transform agent uses this ordering as a hint when fragmenting; out-of-order sources still parse but produce sequence numbers that do not match the conventional reading order.

## Verified

- Code-fence labelling rules informed by `qwen3-embedding:0.6b` retrieval tests on labelled vs unlabelled blocks (verified 2026-04-29). Labelled blocks scored 12% higher on language-specific queries.
- Citation date format matches R5 in `sys-skill-authoring-rules` (verified 2026-05-04).
