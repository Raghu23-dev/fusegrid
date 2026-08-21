# Benchmarks

**This directory is committed and must never be gitignored.**

An uncommitted harness turns a real result into an unverifiable claim — that mistake
has been observed and is not repeated here.

## Layout

```
bench/
  baseline/          measures the problem (step 1)
    results/         raw output, committed as data
  <name>/            measures this system (step 5)
    results/
  run.sh             one command, runs everything
```

## Rules

- **Multiple runs.** Report variance, never a single mean.
- State the **noise floor** — variation observed with no change at all.
- If a comparison is close, state the **minimum detectable difference**.
- Raw output is committed as data so results can be re-analysed without re-running.
