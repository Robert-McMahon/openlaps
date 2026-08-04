# Bench artefacts

The checked-in half of a Phase 4 measurement run. `docs/BENCH_RUNBOOK.md` is
how a run is produced; this is where its evidence lands.

**Raw per-sample data is not here.** `tools/link_probe.py`'s CSVs are large
and local; each manifest records the absolute path of the CSV it describes,
and `.gitignore` keeps them out. What is checked in is the manifest, the
analysis, and the operator's notes — enough that a number in
`docs/LINK_BUDGET.md` has a path back to the run that produced it, and
enough to know whether that run is worth trusting.

## Layout

One directory per run series, for anything with probe output:

```
docs/bench/<date>-<what>/
    summary.md              the analysis; every number cites its manifest
    <role>.manifest.json    one per probe, per condition
    notes.md                the operator's notes, verbatim
```

A package that produces a single write-up and no probe output keeps a flat
`<name>.md` + `<name>.manifest.json` pair. `timing-parity.md` (P4.6) is the
existing example — it crossed no radio, ran no probe and had no operator, so
a directory would have held one document and one manifest.

## The rules that make these worth keeping

**A number without provenance is not a result** (`docs/plan/PHASE4.md` →
ground rules). A figure quoted anywhere in the repository cites the manifest
it came from, or it does not go in.

**Never report a modelled number as measured, and never quietly delete a
caveat.** Where `LINK_BUDGET.md` is updated, measured values go *beside* the
modelled ones so the delta stays visible. A caveat the bench could not close
gets restated, not dropped.

**`notes.md` is not a tidy summary.** Deviations from the runbook, anything
the operator noticed, and anything that went wrong go in verbatim.
`timing-parity.manifest.json` records a docker daemon restart mid-run and a
first run discarded for an encoder bug; that is the standard, and it is the
part a reader three weeks later actually needs.

**No secrets.** Not in a manifest, not in a note, not in a command line
quoted in one.
