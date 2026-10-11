# Session log: 2026-10-07 to 2026-10-11

## State at end of session

- Live site: https://cinnetcrash.github.io/AMRrules_Interactive/ (deploy run 37732660211, commit e76b268, success)
- Local repo: `~/Desktop/AMRrules_Interactive`, branch `main`, clean tree, in sync with `origin/main`
- Upstream: remote `upstream` = AMRverse/AMRrules; fork is 6 commits ahead, 0 behind (no engine/rule files modified)
- Conda env: `amrrules` (Python 3.12, build, pytest, playwright + chromium)
- Tests: `conda run -n amrrules pytest tests/web/test_build.py` (5) and `tests/web/test_parity.py` (12) all pass locally and in CI
- Raw logs/screenshots copied to `logs/` (gitignored): CI watch log, smoke/live Playwright scripts, screenshots

## What was built

1. `scripts/build_web.py` -> `web/build/` (wheel, resources, precomputed CARD map, rules, examples, manifest)
2. `web/worker.js` (Pyodide 0.27.8) + `web/py/runner.py` (glue, obonet stub, CARD map patch, `rules_engine.run()`)
3. `web/index.html`, `web/app.js`, `web/styles.css`: Interpret tab (upload/example, organism or organism file, all CLI options, run log, category matrix, two Tabulator tables, TSV downloads, ruleID links) and Rules tab (per-organism browser, search, PMID links, download)
4. Tests: build tests, CPython glue test, Playwright parity (10 cases byte-for-byte vs CLI, rule link, bad input)
5. CI: `.github/workflows/deploy-pages.yml` (push to main + weekly Monday 03:00 UTC + manual), Pages source = Actions

## Issue found and fixed

- `runner.setup()` wrote resource files into the package `resources/` dir; on CPython with the editable install that is the source tree, and `read_text()` had normalised CRLF->LF, so tests rewrote `amrfp_to_card_drugs_classes.txt`. Fixed: write only when bytes differ; tests read bytes.

## Expansion ideas discussed (not started)

Recommended first round (all outside the engine, parity test unaffected):
- AST concordance module: upload sample x antibiotic S/I/R (or MIC) table, compute categorical agreement / major error / very major error against AMRrules categories, highlight discordant cells in the matrix
- Multi-sample summary: R count per sample, R/I/S per drug class (bar chart), MDR/XDR classification, list of markers with no rule
- Rule detail side panel on ruleID click (breakpoint, evidence code, PMID, curation note; ARO -> CARD, nodeID -> NCBI RefGene links)

Second round:
- PWA/service worker for offline use after first visit
- Rule-set version selector (build wheels from upstream tags v1.0.0, v1.0.1, ...)
- Rules dashboard (rules per organism, drug class coverage, evidence grade distribution)
- Standalone HTML report export; keep past runs in IndexedDB

Not feasible / belongs upstream:
- FASTA input (AMRFinderPlus cannot run in the browser; would need a server)
- CARD RGI / ResFinder input (engine change; contribute to AMRverse instead of forking behaviour)

## To resume

```bash
cd ~/Desktop/AMRrules_Interactive
git fetch upstream && git status
conda run -n amrrules python scripts/build_web.py --skip-download
conda run -n amrrules pytest tests/web -q
make serve-web   # http://localhost:8000/
```
