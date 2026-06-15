# Observathon Submission Report

## Goal

Target the maximum private score by improving every scored dimension without hardcoding answers, prices, question IDs, seeds, or scorer behavior.

## Submission Files

- `solution/config.json`: conservative runtime knobs for deterministic, low-cost, guarded execution.
- `solution/prompt.txt`: short checkout policy focused on grounding, exact arithmetic, PII safety, tool economy, and injection resistance.
- `solution/examples.json`: behavior examples only; no memorized prices or public/private IDs.
- `solution/wrapper.py`: observability plus retry, cache, input sanitization, output redaction, and prompt routing.
- `solution/findings.json`: diagnosis entries for latency, arithmetic, prompt injection, and PII leakage.

## Score Strategy

- Correctness: tool-first prompt, refusal on unknown/out-of-stock/unsupported cases, exact total format.
- Quality: concise answers, no fabricated totals, clear refusal path.
- Error rate: retry and loop guard enabled; wrapper catches exceptions and records the failure mode.
- Latency: max steps, tool budget, context size, cache, and short completion cap reduce long tails.
- Cost: small model, short prompt, low context, low completion cap, cache.
- Drift: context reset and low temperature reduce session contamination.
- Prompt: explicit grounding, arithmetic formula, PII rule, and injection defense while staying well below the 3000 char limit.
- Diagnosis F1: findings map directly to documented fault classes and include evidence, root cause, and fix.

## Verification

Run:

```powershell
python harness/selfcheck.py
python -m unittest tests/test_submission.py
```

Expected:

- selfcheck passes `config.json`, `wrapper.py`, `prompt.txt`, `examples.json`, and `findings.json`.
- unit tests pass offline without an API key.

## Runtime Note

The Windows practice binary originally failed before the simulator entered Python because PyInstaller loaded a bundled runtime DLL incorrectly. A patched runtime was prepared under `C:\obs-run` during debugging. The submission itself remains focused on the legal `solution/` files and offline validation.
