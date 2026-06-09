# Curated URL List — Authoring Prompt

Use this prompt with a local LLM to draft `curated/<language>.yaml`.
Replace `{LANGUAGE}` and `{VERSION_ANCHOR}` before sending. After the model returns,
**you (the human) must validate every URL with a HEAD request before committing** —
local models hallucinate plausible-looking URLs.

---

## Prompt

You are producing a curated URL index for the **{LANGUAGE}** programming language,
version anchor **{VERSION_ANCHOR}** (e.g., "Python 3.13", "Java 21 LTS", "Go 1.23").

Your output is consumed by a downstream skill-authoring pipeline that needs to
cite primary sources. The list will be reviewed by a human and every URL will be
verified with a HEAD request, so:

**Hard constraints:**
1. Emit ONLY URLs you are highly confident exist on the canonical documentation
   domain for {LANGUAGE}. Do not invent path segments, anchor fragments, or
   version-specific paths you have not seen.
2. Canonical domains only. Allowed examples by language:
   - Python: docs.python.org, peps.python.org
   - Java: docs.oracle.com/en/java, openjdk.org/jeps
   - Go: go.dev/doc, go.dev/ref, pkg.go.dev/std
   - Rust: doc.rust-lang.org, rust-lang.github.io/api-guidelines
   - Node.js: nodejs.org/api, nodejs.org/en/learn
   - TypeScript: typescriptlang.org/docs
3. **Forbidden sources**: Stack Overflow, Medium, dev.to, personal blogs,
   tutorials sites, GitHub READMEs (unless it's the language's own canonical
   spec repo), Wikipedia, video transcripts, anything paywalled.
4. Prefer stable / LTS version paths over `/latest/` redirects.
5. If you are not sure a URL exists, omit it. Coverage gaps are fine; fabricated
   URLs are not.

**Output format** — emit only valid YAML, no prose, no markdown fences:

```yaml
language: {LANGUAGE}
version_anchor: {VERSION_ANCHOR}
generated_at: 2026-05-04
canonical:
  language_reference: <single most authoritative reference URL>
  library_reference: <stdlib / standard library index URL>
  spec_or_standard: <formal spec URL if one exists, else null>
  release_notes: <release notes index URL>
topics:
  # 8–12 topic areas typical for skill authoring. For each, 2–5 URLs.
  # Topics MUST be drawn from this list; omit any topic where no canonical
  # source exists for {LANGUAGE}:
  #   - concurrency_and_async
  #   - error_handling_and_exceptions
  #   - typing_and_generics
  #   - memory_and_performance
  #   - packaging_and_dependency_management
  #   - testing_idioms
  #   - security_and_crypto_primitives
  #   - io_and_filesystem
  #   - networking_and_http_client
  #   - serialization_json_binary
  #   - logging_and_observability
  #   - language_evolution_and_deprecation
  <topic_name>:
    description: <one sentence on what skill authors look up here>
    urls:
      - <url>
      - <url>
known_gaps:
  # 1–5 honest notes on areas where the canonical docs are weak or absent
  # for {LANGUAGE}, so authors know to supplement.
  - <one-line gap description>
```

**Discipline check before emitting:**
- Did you include any URL you have not seen in training data? If yes, remove it.
- Are all URLs on canonical domains? If not, remove them.
- Did you fabricate version numbers or path fragments? If yes, fix or remove.

Emit the YAML now.

---

## Verification step (run after the model emits)

```bash
# Save model output to candidate.yaml, then:
cd <repo-root>/skill-source/upstream/curated
python3 -c "
import yaml, urllib.request, sys
data = yaml.safe_load(open('candidate.yaml'))
urls = []
for k, v in (data.get('canonical') or {}).items():
    if v: urls.append((f'canonical.{k}', v))
for topic, body in (data.get('topics') or {}).items():
    for u in (body.get('urls') or []):
        urls.append((f'topics.{topic}', u))
fail = []
for label, url in urls:
    req = urllib.request.Request(url, method='HEAD')
    try:
        r = urllib.request.urlopen(req, timeout=8)
        ok = 200 <= r.status < 400
    except Exception as e:
        ok = False
    print(('OK ' if ok else 'XX '), label, url)
    if not ok: fail.append((label, url))
sys.exit(1 if fail else 0)
"
```

Anything marked `XX` either was hallucinated, has moved, or needs a different
canonical path. Fix or remove before committing the list as `<language>.yaml`.
