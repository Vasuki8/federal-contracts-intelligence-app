# Incumbent matching: evaluation

No labels yet. Once the labeling spreadsheet is filled in (README, "Labeling matches"),
`uv run app match eval --report docs/matching-eval.md` (or GitHub → Actions → Ingest →
`match-eval`) replaces this file with precision, recall, a threshold sweep and a check on
notices that cite their incumbent's contract number. Target: ≥ 90% precision on shown
incumbents.
