# Benchmarks

## Composed vs Flat

The POC comparing AgentAlloy's just-in-time composed injection against flat (all-skills-at-once) injection is documented in [docs/experiments/poc-composed-vs-flat.md](docs/experiments/poc-composed-vs-flat.md).

**Aspirational targets** (not yet measured): 60% smaller prompts, 25% faster runs, improved answers.

Run the experiment:

```bash
AGENT_MODEL=<your-agent-model> uv run python -m eval.run_poc --n 3
```

Requires running AgentAlloy service and a local agent model via LM Studio.

## Retrieval Recall

The recall@k harness measures retrieval quality without any agent model:

```bash
uv run python -m eval.recall --k 4
```

See [docs/experiments/poc-composed-vs-flat.md §6](docs/experiments/poc-composed-vs-flat.md) for details.
