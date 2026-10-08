# AMRrules Interactive: browser-based interpretation on GitHub Pages

Date: 2026-10-08
Status: approved design, pending implementation

## Goal

Make the AMRrules interpretation engine usable from a web page hosted on GitHub Pages
(static hosting, no server), so that anyone can upload AMRFinderPlus output, pick an
organism, and get the interpreted genotype report and genome summary report
interactively in the browser. A second tab lets users browse the organism rule sets.

Everything runs client-side. Uploaded genotype data never leaves the user's machine.
This matters because inputs are often clinical or surveillance isolates.

## Non-goals (v1)

- Running AMRFinderPlus itself (FASTA input). Input is AMRFinderPlus TSV only.
- Editing or proposing rules. Rule browser is read-only.
- Any server component, database, or user accounts.
- Docker image. A static site needs none; local preview is `python -m http.server`.
- Changing engine behaviour. The fork stays mergeable with upstream AMRverse/AMRrules.

## Approaches considered

1. **Pyodide + custom HTML/JS UI (chosen).** The unmodified `amrrules` wheel runs in
   the browser via Pyodide (CPython compiled to WebAssembly). Zero engine rewrite,
   stays in sync with upstream, output identical to the CLI. Cost: ~10 MB Pyodide
   runtime download on first visit (cached afterwards), a few seconds startup.
2. **stlite (Streamlit in browser).** Fastest to write, but 2-3x larger payload,
   slower startup, limited control over table interactivity and layout.
3. **Port the engine to JavaScript.** Smallest and fastest at runtime, but ~1500 lines
   to rewrite and re-validate, and every upstream rule-engine change must be ported.
   Rejected: parity risk and maintenance burden outweigh the speed gain.

## Architecture

```
GitHub repo (fork of AMRverse/AMRrules)
├── src/amrrules/            unchanged engine
├── rules/*.tsv              unchanged rule sets
├── web/                     static site (new)
│   ├── index.html           single page, two tabs: Interpret, Rules
│   ├── app.js               UI logic, tables, downloads
│   ├── worker.js            Pyodide runs here (Web Worker, UI never blocks)
│   ├── styles.css
│   └── build/               generated at build time, gitignored:
│       ├── amrrules-<ver>-py3-none-any.whl
│       ├── resources/ReferenceGeneHierarchy.txt, version.txt,
│       │             card_drug_class_map.json
│       ├── rules/*.tsv  + rules_index.json
│       └── examples/*.tsv (copied from tests/data/input)
├── scripts/build_web.py     builds web/build/ (new)
├── tests/web/test_parity.py Playwright parity test (new)
└── .github/workflows/deploy-pages.yml (new)
```

### Build step (`scripts/build_web.py`)

Runs in CI and locally (`make web`):

1. `python copy_rules.py` then `python -m build --wheel` to produce the wheel.
2. `amrrules --download-resources` to fetch the current AMRFinderPlus
   ReferenceGeneHierarchy and CARD archives.
3. Instantiate `ResourceManager`, call `get_card_drug_class_map()`, dump the result to
   `card_drug_class_map.json`. This removes the need to ship the 3 MB `aro.obo` and to
   parse it with obonet in the browser on every visit.
4. Copy `ReferenceGeneHierarchy.txt`, `version.txt`, cleaned `rules/*.tsv`, the
   `rule_key_file.tsv`, and example inputs into `web/build/`.
5. Write `web/build/manifest.json`: amrrules version, AMRFinderPlus DB version, CARD
   version (from the download URL), build timestamp, git commit. The UI shows these.

### Runtime (browser)

- `worker.js` loads Pyodide from the jsDelivr CDN (pinned version, Python 3.12
  compatible), installs the local wheel with micropip, writes the resource files into
  the package's `resources/` directory in the virtual filesystem, and monkeypatches
  `ResourceManager.get_card_drug_class_map` to return the precomputed JSON.
  `obonet` stays an import-time dependency of `resources.py`, so a minimal stub
  module is written to the virtual FS instead of installing networkx. The stub is
  never called because the patched method returns before using it.
- On "Run", the worker writes the uploaded TSV (and optional organism file) to the
  virtual FS, builds an `argparse.Namespace` with the same fields as the CLI, calls
  `rules_engine.run(args)`, captures stdout, and reads the two output TSVs back.
  Results go to the main thread as text.
- Nothing is ever sent over the network except the static assets themselves.

### UI

**Interpret tab**
- Input: file picker (TSV or TSV.gz, drag-and-drop) or "load example" dropdown
  populated from `examples/`.
- Mode: single organism (select from supported list) or multi-sample
  (upload organism file, two columns, no header, same as CLI).
- Options mirror the CLI: `--no-rule-interpretation` (none/nwt/nwtR/nwtS),
  `--annot-opts` (minimal/full), `--flag-core`, `--full-disrupt`, `--print-non-amr`,
  optional `--sample-id`.
- Output: run summary block (samples processed/skipped, markers matched/unmatched,
  versions), then two interactive tables (Tabulator: sort, per-column filter, column
  show/hide) for the genome summary and the interpreted genotype report, and a
  "Category matrix" view: samples as rows, drug or drug class as columns, cell coloured
  by clinical category (S/I/R/no rule). Download buttons give the exact TSVs the CLI
  would produce, named `<prefix>_interpreted.tsv` and `<prefix>_genome_summary.tsv`.
- Clicking a ruleID in either table switches to the Rules tab filtered to that rule.

**Rules tab**
- Organism selector, Tabulator table of that rule file with per-column filters and
  free-text search; link out to PMIDs and accessions. Download the TSV.

**Header/footer**: AMRrules version, AMRFinderPlus DB version, CARD version, build
date, link to the upstream repo and docs, and a one-line privacy statement
("Files are processed in your browser and are not uploaded anywhere").

### Error handling

- Engine errors (`SystemExit`, `parser.error`-style validation, wrong columns,
  sample IDs missing from organism file) are caught in the worker and shown verbatim
  in an error panel together with captured stdout/stderr. No silent failure.
- Pyodide/CDN load failure shows a clear message with a retry button.
- Large inputs: the worker keeps the UI responsive; a progress line shows phases
  (loading runtime, installing package, running, rendering).

### Testing

- `tests/web/test_build.py` (pytest): `build_web.py` produces every expected file and
  the JSON map is non-empty and contains known keys (e.g. `ciprofloxacin`).
- `tests/web/test_parity.py` (pytest + Playwright, Chromium headless): serve
  `web/` locally, run each example input in the browser with the same options the CI
  workflow uses for `tests/data/example_output`, download the two TSVs, and compare
  byte-for-byte with the CLI output generated in the same environment. Any difference
  fails the build.
- CI runs both before deploying.

### Deployment

- `.github/workflows/deploy-pages.yml`: on push to `main` and weekly on a schedule
  (to pick up new AMRFinderPlus/CARD resource versions), run build + tests, upload
  `web/` as the Pages artifact, deploy with `actions/deploy-pages`.
- GitHub Pages source set to "GitHub Actions" on the fork.
- Site URL: `https://cinnetcrash.github.io/AMRrules_Interactive/`.

### Keeping in sync with upstream

No engine or rule files are modified. Upstream `main` can be merged at any time;
the web layer only depends on the CLI argument surface and the two output files.
If upstream changes that surface, the parity test fails and points to the break.

## Open items

None blocking. Pyodide version will be pinned at implementation time to the newest
release that ships Python 3.12.
