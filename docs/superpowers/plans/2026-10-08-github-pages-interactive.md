# AMRrules Interactive (GitHub Pages) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the unmodified AMRrules engine as an interactive, client-side web app on GitHub Pages, with byte-identical output to the CLI and a read-only rule browser.

**Architecture:** A build script produces a wheel plus precomputed resource files in `web/build/`. A Web Worker loads Pyodide 0.27.8, installs the wheel, writes the resources into the package's virtual filesystem, and calls `rules_engine.run()` with an `argparse.Namespace` identical to the CLI's. The page (vanilla JS + Tabulator) renders the two output TSVs, a clinical-category matrix, and the rule files. A Playwright parity test compares browser output with CLI output byte-for-byte; a GitHub Actions workflow builds, tests and deploys to Pages.

**Tech Stack:** Python 3.12 (`build`, `pytest`, `playwright`), Pyodide 0.27.8 (CDN, Python 3.12.7), Tabulator 6.4.0 (cdnjs), GitHub Actions `upload-pages-artifact`/`deploy-pages`.

**Spec:** `docs/superpowers/specs/2026-10-08-github-pages-interactive-design.md`

**Environment:** conda env `amrrules` (Python 3.12). Run every command as `conda run -n amrrules <cmd>` from the repo root, or activate the env first. Commits use the attribution trailer configured for this session.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/build_web.py` (new) | Build wheel, download resources, precompute CARD map, copy rules/examples, write `web/build/manifest.json` |
| `web/index.html` (new) | Page skeleton: header, two tabs, form, result containers, footer |
| `web/styles.css` (new) | Layout and colours (light/dark aware) |
| `web/app.js` (new) | UI logic: load manifest/indices, talk to worker, render tables/matrix, downloads, rule browser |
| `web/worker.js` (new) | Pyodide lifecycle and message protocol only |
| `web/py/runner.py` (new) | Python glue run inside Pyodide: stub obonet, place resources, patch CARD map, run engine |
| `web/.nojekyll` (new) | Disable Jekyll on Pages |
| `web/build/` (generated, gitignored) | wheel, `resources/`, `rules/`, `examples/`, `manifest.json`, `rules_index.json`, `examples_index.json` |
| `tests/web/test_build.py` (new) | Build output checks |
| `tests/web/test_parity.py` (new) | Playwright: browser output == CLI output |
| `.github/workflows/deploy-pages.yml` (new) | Build, test, deploy |
| `Makefile` (modify) | `web` and `serve-web` targets |
| `.gitignore` (modify) | add `web/build/` |
| `README.md` (modify) | section on the interactive site |

---

### Task 1: Build script and its tests

**Files:**
- Create: `scripts/build_web.py`
- Create: `tests/web/test_build.py`
- Modify: `.gitignore`, `Makefile`

- [ ] **Step 1: Write the failing tests**

`tests/web/test_build.py`:

```python
"""Checks that scripts/build_web.py produces every asset the browser UI needs."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "web" / "build"
PKG_RES = ROOT / "src" / "amrrules" / "resources"
DOWNLOADED = ["aro.obo", "aro_categories.tsv", "ReferenceGeneHierarchy.txt", "version.txt"]


@pytest.fixture(scope="session")
def built():
    have_resources = all((PKG_RES / f).exists() for f in DOWNLOADED)
    cmd = [sys.executable, str(ROOT / "scripts" / "build_web.py")]
    if have_resources:
        cmd.append("--skip-download")
    subprocess.run(cmd, check=True, cwd=ROOT)
    return BUILD


def test_expected_files(built):
    manifest = json.loads((built / "manifest.json").read_text())
    assert (built / manifest["wheel"]).exists()
    for key in ["amrrules_version", "amrfp_db_version", "card_version", "built_at", "git_commit"]:
        assert manifest[key], key
    for rel in [
        "resources/ReferenceGeneHierarchy.txt",
        "resources/version.txt",
        "resources/amrfp_to_card_drugs_classes.txt",
        "resources/card_drug_class_map.json",
        "rules/rule_key_file.tsv",
        "rules/Escherichia_coli.tsv",
        "rules_index.json",
        "examples/test_ecoli_wildtype.tsv",
        "examples_index.json",
    ]:
        assert (built / rel).exists(), rel


def test_card_map_content(built):
    card_map = json.loads((built / "resources" / "card_drug_class_map.json").read_text())
    assert len(card_map) > 100
    assert card_map.get("ciprofloxacin")
    assert card_map.get("kanamycin"), "engine adds a kanamycin alias; it must survive precomputation"


def test_rules_index(built):
    index = json.loads((built / "rules_index.json").read_text())
    assert index["s__Escherichia coli"] == "Escherichia_coli.tsv"
    assert len(index) >= 20
    for filename in index.values():
        assert (built / "rules" / filename).exists(), filename
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n amrrules pytest tests/web/test_build.py -v`
Expected: ERROR in fixture `built` (`scripts/build_web.py` not found).

- [ ] **Step 3: Write the build script**

`scripts/build_web.py`:

```python
#!/usr/bin/env python
"""Build the static assets for the browser UI into web/build/.

Steps: clean+copy rules, build the wheel, download AMRFinderPlus/CARD resources
(unless --skip-download), precompute the CARD drug->class map, copy resources,
rules and example inputs, write manifest.json.

Run from the repository root:  python scripts/build_web.py [--skip-download]
"""
import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_BUILD = ROOT / "web" / "build"
PKG_RES = ROOT / "src" / "amrrules" / "resources"
PKG_RULES = ROOT / "src" / "amrrules" / "rules"
EXAMPLES = ROOT / "tests" / "data" / "input"

# Files the browser needs verbatim. aro.obo is NOT shipped: its content is
# precomputed into card_drug_class_map.json.
RESOURCE_FILES = ["ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt"]
CARD_SOURCE_FILES = ["aro.obo", "aro_categories.tsv"]


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def card_version():
    """CARD version is hard-coded in the engine's download URL; read it from there."""
    src = (ROOT / "src" / "amrrules" / "resources.py").read_text()
    match = re.search(r"ontology-v([\d.]+)\.tar\.bz2", src)
    return match.group(1) if match else "unknown"


def build_wheel(out_dir: Path) -> str:
    for old in out_dir.glob("amrrules-*.whl"):
        old.unlink()
    run([sys.executable, "copy_rules.py"])
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)])
    wheels = sorted(out_dir.glob("amrrules-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {out_dir}, found {wheels}")
    return wheels[0].name


def replace_tree(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-download", action="store_true", help="reuse resource files already present in src/amrrules/resources")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    from amrrules import __version__
    from amrrules.resources import ResourceManager

    WEB_BUILD.mkdir(parents=True, exist_ok=True)
    wheel = build_wheel(WEB_BUILD)

    manager = ResourceManager()
    if not args.skip_download:
        if not manager.setup_all_resources():
            raise SystemExit("resource download failed")
    missing = [f for f in RESOURCE_FILES + CARD_SOURCE_FILES if not (PKG_RES / f).exists()]
    if missing:
        raise SystemExit(f"missing resource files: {missing} (run without --skip-download)")

    res_out = WEB_BUILD / "resources"
    res_out.mkdir(exist_ok=True)
    for name in RESOURCE_FILES:
        shutil.copy(PKG_RES / name, res_out / name)

    card_map = manager.get_card_drug_class_map()
    if not card_map:
        raise SystemExit("CARD drug class map is empty")
    (res_out / "card_drug_class_map.json").write_text(json.dumps(card_map, sort_keys=True))

    replace_tree(PKG_RULES, WEB_BUILD / "rules")
    index = {}
    for line in (PKG_RULES / "rule_key_file.tsv").read_text().splitlines():
        if line.strip():
            organism, stem = line.split("\t")
            index[organism] = stem + ".tsv"
    (WEB_BUILD / "rules_index.json").write_text(json.dumps(dict(sorted(index.items())), indent=1))

    replace_tree(EXAMPLES, WEB_BUILD / "examples")
    (WEB_BUILD / "examples_index.json").write_text(json.dumps(sorted(p.name for p in (WEB_BUILD / "examples").glob("*.tsv"))))

    manifest = {
        "amrrules_version": __version__,
        "wheel": wheel,
        "amrfp_db_version": manager.get_amrfp_db_version(),
        "card_version": card_version(),
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
    }
    (WEB_BUILD / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add `web/build/` to `.gitignore` and Makefile targets**

Append to `.gitignore`:

```
# generated browser assets
web/build/
```

Append to `Makefile` (and add `web serve-web` to `.PHONY`):

```make
# Build static assets for the browser UI (web/build/)
web:
	python scripts/build_web.py

# Preview the browser UI locally
serve-web:
	python -m http.server -d web 8000
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n amrrules pytest tests/web/test_build.py -v`
Expected: 3 passed. Also check `unzip -l web/build/amrrules-*.whl | grep -c rules/` lists 21 rule files including `rule_key_file.tsv`.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_web.py tests/web/test_build.py .gitignore Makefile
git commit -m "build: add web asset build script with tests"
```

---

### Task 2: Python glue and Pyodide worker

**Files:**
- Create: `web/py/runner.py`
- Create: `web/worker.js`

- [ ] **Step 1: Write a CPython test for the glue (it must also run outside Pyodide)**

Append to `tests/web/test_build.py`:

```python
def test_runner_glue_matches_cli(built, tmp_path):
    """runner.py is the code that runs in the browser; exercise it on CPython too."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("runner", ROOT / "web" / "py" / "runner.py")
    runner = importlib.util.module_from_spec(spec)
    runner.WORK = tmp_path / "work"
    runner.IN = runner.WORK / "in"
    runner.OUT = runner.WORK / "out"
    spec.loader.exec_module(runner)
    runner.WORK, runner.IN, runner.OUT = tmp_path / "work", tmp_path / "work" / "in", tmp_path / "work" / "out"

    resources = {name: (built / "resources" / name).read_text() for name in
                 ["ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt", "card_drug_class_map.json"]}
    info = json.loads(runner.setup(resources))
    assert "s__Escherichia coli" in info["organisms"]

    input_path = ROOT / "tests" / "data" / "input" / "test_ecoli_wildtype.tsv"
    result = json.loads(runner.run(json.dumps({"organism": "s__Escherichia coli", "output_prefix": "glue"}),
                                   input_path.name, input_path.read_bytes(), None))
    assert result["ok"], result
    assert "AMRrules complete" in result["log"]

    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    subprocess.run([sys.executable, "-m", "amrrules", "--input", str(input_path), "--output-prefix", "glue",
                    "--organism", "s__Escherichia coli", "--output-dir", str(cli_dir)], check=True, cwd=ROOT)
    assert result["interpreted"] == (cli_dir / "glue_interpreted.tsv").read_bytes().decode()
    assert result["summary"] == (cli_dir / "glue_genome_summary.tsv").read_bytes().decode()


def test_runner_reports_engine_errors(built, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("runner", ROOT / "web" / "py" / "runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.WORK, runner.IN, runner.OUT = tmp_path / "work", tmp_path / "work" / "in", tmp_path / "work" / "out"
    resources = {name: (built / "resources" / name).read_text() for name in
                 ["ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt", "card_drug_class_map.json"]}
    runner.setup(resources)
    result = json.loads(runner.run(json.dumps({"organism": "s__Escherichia coli"}), "bad.tsv", b"not\ta\tvalid\tfile\n", None))
    assert result["ok"] is False
    assert result["error"]
```

Note: the first test sets `WORK/IN/OUT` after `exec_module` (the assignments before it are harmless and may be removed); the second shows the minimal form.

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n amrrules pytest tests/web/test_build.py -v -k runner`
Expected: FAIL, `FileNotFoundError: web/py/runner.py`.

- [ ] **Step 3: Write `web/py/runner.py`**

```python
"""Glue between worker.js and the amrrules engine. Executed inside Pyodide.

Also importable on CPython so the build tests can exercise it.
"""
import argparse
import contextlib
import io
import json
import re
import sys
import traceback
from pathlib import Path

WORK = Path("/work")
IN = WORK / "in"
OUT = WORK / "out"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESOURCE_TEXT_FILES = ("ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt")


def _install_stub_obonet():
    """resources.py imports obonet at module level. The browser build never parses
    aro.obo (the CARD map is precomputed), so a stub keeps the import working
    without shipping obonet + networkx."""
    try:
        import obonet  # noqa: F401  (present on CPython in the dev env)
        return
    except ImportError:
        pass
    stub_dir = WORK / "stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "obonet.py").write_text(
        "def read_obo(*args, **kwargs):\n"
        "    raise RuntimeError('obonet is not available in the browser build; the CARD map is precomputed')\n"
    )
    if str(stub_dir) not in sys.path:
        sys.path.insert(0, str(stub_dir))


def setup(resources):
    """resources: mapping filename -> text for the files in web/build/resources/.
    Returns JSON with the engine version and supported organisms."""
    resources = dict(resources)
    _install_stub_obonet()
    import amrrules.resources as res_mod

    res_dir = Path(res_mod.__file__).parent / "resources"
    res_dir.mkdir(exist_ok=True)
    for name in RESOURCE_TEXT_FILES:
        (res_dir / name).write_text(resources[name])

    card_map = json.loads(resources["card_drug_class_map.json"])
    res_mod.ResourceManager.get_card_drug_class_map = lambda self: card_map

    from amrrules import __version__
    from amrrules.utils import get_supported_organisms

    return json.dumps({"version": __version__, "organisms": get_supported_organisms()})


def _to_bytes(data):
    if hasattr(data, "to_bytes"):      # Pyodide JsProxy of a Uint8Array
        return data.to_bytes()
    return bytes(data)


def _reset_dirs():
    for d in (IN, OUT):
        d.mkdir(parents=True, exist_ok=True)
        for p in d.iterdir():
            p.unlink()


def run(opts_json, input_name, input_bytes, organism_file_text=None):
    """Run the engine exactly as the CLI would. Returns JSON:
    {ok, log, error, interpreted, summary, prefix}."""
    from amrrules import rules_engine

    opts = json.loads(opts_json)
    _reset_dirs()
    in_path = IN / Path(input_name).name
    in_path.write_bytes(_to_bytes(input_bytes))
    org_path = None
    if organism_file_text is not None:
        org_path = IN / "organisms.tsv"
        org_path.write_text(organism_file_text)

    prefix = opts.get("output_prefix") or "amrrules"
    args = argparse.Namespace(
        input=str(in_path),
        output_prefix=prefix,
        output_dir=str(OUT),
        sample_id=opts.get("sample_id") or None,
        organism=None if org_path else opts.get("organism"),
        organism_file=str(org_path) if org_path else None,
        amr_tool="amrfp",
        no_rule_interpretation=opts.get("no_rule_interpretation", "none"),
        annot_opts=opts.get("annot_opts", "minimal"),
        flag_core=bool(opts.get("flag_core")),
        full_disrupt=bool(opts.get("full_disrupt")),
        print_non_amr=bool(opts.get("print_non_amr")),
    )

    result = {"ok": False, "log": "", "error": None, "interpreted": None, "summary": None, "prefix": prefix}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rules_engine.run(args)
            result["ok"] = True
        except SystemExit as exc:
            result["error"] = str(exc) or "AMRrules exited without a message"
        except Exception:
            result["error"] = traceback.format_exc()
    result["log"] = ANSI.sub("", buf.getvalue())
    if result["ok"]:
        # read as bytes: the engine writes \r\n and the browser download must match the CLI byte-for-byte
        result["interpreted"] = (OUT / f"{prefix}_interpreted.tsv").read_bytes().decode("utf-8")
        result["summary"] = (OUT / f"{prefix}_genome_summary.tsv").read_bytes().decode("utf-8")
    return json.dumps(result)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n amrrules pytest tests/web/test_build.py -v`
Expected: 5 passed.

- [ ] **Step 5: Write `web/worker.js`**

```js
/* Web Worker: owns the Pyodide runtime so the page never blocks.
 * Protocol (main -> worker): {type:"init", baseUrl, manifest} | {type:"run", id, opts, inputName, inputBytes, organismFileText}
 * Protocol (worker -> main): {type:"phase", text} | {type:"ready", info} | {type:"result", id, result} | {type:"error", id, message}
 */
const PYODIDE_VERSION = "0.27.8";
const PYODIDE_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const RESOURCE_NAMES = ["ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt", "card_drug_class_map.json"];

let pyodide = null;
let pyRun = null;

function post(type, payload) {
  self.postMessage(Object.assign({ type }, payload || {}));
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} while fetching ${url}`);
  return response.text();
}

async function init(baseUrl, manifest) {
  post("phase", { text: `Loading Python runtime (Pyodide ${PYODIDE_VERSION})` });
  importScripts(PYODIDE_URL + "pyodide.js");
  pyodide = await loadPyodide({ indexURL: PYODIDE_URL });

  post("phase", { text: `Installing amrrules ${manifest.amrrules_version}` });
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  await micropip.install.callKwargs(new URL("build/" + manifest.wheel, baseUrl).href, { deps: false });

  post("phase", { text: "Loading reference data" });
  const texts = await Promise.all(RESOURCE_NAMES.map((name) => fetchText(new URL("build/resources/" + name, baseUrl).href)));
  const resources = {};
  RESOURCE_NAMES.forEach((name, i) => { resources[name] = texts[i]; });

  pyodide.runPython(await fetchText(new URL("py/runner.py", baseUrl).href));
  const setup = pyodide.globals.get("setup");
  const info = JSON.parse(setup(pyodide.toPy(resources)));
  setup.destroy();
  pyRun = pyodide.globals.get("run");
  post("ready", { info });
}

self.onmessage = async (event) => {
  const msg = event.data;
  try {
    if (msg.type === "init") {
      await init(msg.baseUrl, msg.manifest);
    } else if (msg.type === "run") {
      if (!pyRun) throw new Error("Python runtime is not ready yet");
      post("phase", { text: "Running AMRrules" });
      const raw = pyRun(JSON.stringify(msg.opts), msg.inputName, pyodide.toPy(msg.inputBytes), msg.organismFileText ?? null);
      post("result", { id: msg.id, result: JSON.parse(raw) });
    }
  } catch (err) {
    post("error", { id: msg.id, message: String((err && err.message) || err) });
  }
};
```

- [ ] **Step 6: Commit**

```bash
git add web/py/runner.py web/worker.js tests/web/test_build.py
git commit -m "web: add Pyodide worker and Python glue for running the engine in-browser"
```

---

### Task 3: Page, styles and application logic

**Files:**
- Create: `web/index.html`, `web/styles.css`, `web/app.js`, `web/.nojekyll`

- [ ] **Step 1: Write `web/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AMRrules Interactive</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabulator/6.4.0/css/tabulator_simple.min.css">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
<header>
  <div class="header-row">
    <h1>AMRrules Interactive</h1>
    <div id="status" class="status loading" aria-live="polite">Starting…</div>
  </div>
  <p class="tagline">Organism-specific interpretation of AMRFinderPlus genotypes. Everything runs in your browser; your files are never uploaded.</p>
  <nav class="tabs" role="tablist">
    <button type="button" data-tab="interpret" class="active" role="tab">Interpret</button>
    <button type="button" data-tab="rules" role="tab">Rules</button>
  </nav>
</header>

<main>
  <section id="tab-interpret" class="tab active">
    <form id="run-form">
      <fieldset>
        <legend>1. Input: AMRFinderPlus output (TSV or TSV.gz)</legend>
        <div class="row">
          <input type="file" id="input-file" accept=".tsv,.txt,.gz,text/tab-separated-values,text/plain">
          <label>or load an example
            <select id="example-input"><option value="">—</option></select>
          </label>
        </div>
        <div id="input-name" class="muted"></div>
      </fieldset>

      <fieldset>
        <legend>2. Organism</legend>
        <div class="row">
          <label><input type="radio" name="mode" value="single" checked> Single organism</label>
          <select id="organism"></select>
        </div>
        <div class="row">
          <label><input type="radio" name="mode" value="multi"> Organism file (sample ID &nbsp;TAB&nbsp; organism, no header)</label>
          <input type="file" id="organism-file" accept=".tsv,.txt">
          <label>or example
            <select id="example-orgfile"><option value="">—</option></select>
          </label>
        </div>
        <div id="orgfile-name" class="muted"></div>
      </fieldset>

      <fieldset>
        <legend>3. Options (same as the command line)</legend>
        <div class="row">
          <label>No-rule interpretation
            <select id="opt-nr">
              <option value="none">none</option>
              <option value="nwt">nwt</option>
              <option value="nwtR">nwtR</option>
              <option value="nwtS">nwtS</option>
            </select>
          </label>
          <label>Annotation
            <select id="opt-annot">
              <option value="minimal">minimal</option>
              <option value="full">full</option>
            </select>
          </label>
          <label>Output prefix <input type="text" id="opt-prefix" value="amrrules" size="12"></label>
          <label>Sample ID (single-sample files) <input type="text" id="opt-sample-id" size="14"></label>
        </div>
        <div class="row">
          <label><input type="checkbox" id="opt-flag-core"> Flag core genes</label>
          <label><input type="checkbox" id="opt-full-disrupt"> Show full POINT_DISRUPT mutation</label>
          <label><input type="checkbox" id="opt-non-amr"> Include non-AMR rows</label>
        </div>
      </fieldset>

      <div class="row">
        <button type="submit" id="run-btn" disabled>Run AMRrules</button>
        <span id="run-status" class="muted"></span>
      </div>
    </form>

    <div id="error" class="error hidden" role="alert"></div>

    <section id="results" class="hidden">
      <details>
        <summary>Run log</summary>
        <pre id="run-log"></pre>
      </details>

      <div class="result-block">
        <h2>Genome summary report <button type="button" id="dl-summary">Download TSV</button></h2>
        <div class="row matrix-controls">
          <label>Category matrix by
            <select id="matrix-level">
              <option value="class">drug class</option>
              <option value="drug">drug</option>
            </select>
          </label>
          <span class="legend"><i class="cat-S"></i>S <i class="cat-I"></i>I <i class="cat-R"></i>R <i class="cat-none"></i>no category</span>
        </div>
        <div id="matrix" class="matrix-wrap"></div>
        <div id="table-summary"></div>
      </div>

      <div class="result-block">
        <h2>Interpreted genotype report <button type="button" id="dl-interpreted">Download TSV</button></h2>
        <div id="table-interpreted"></div>
      </div>
    </section>
  </section>

  <section id="tab-rules" class="tab">
    <div class="row">
      <label>Organism <select id="rules-organism"></select></label>
      <input type="search" id="rules-search" placeholder="Search all columns">
      <button type="button" id="dl-rules">Download TSV</button>
      <span id="rules-count" class="muted"></span>
    </div>
    <div id="table-rules"></div>
  </section>
</main>

<footer>
  <div id="versions"></div>
  <div>
    <a href="https://github.com/AMRverse/AMRrules">AMRrules (upstream)</a> ·
    <a href="https://amrrules.readthedocs.io/">Documentation</a> ·
    <a href="https://github.com/cinnetcrash/AMRrules_Interactive">Source of this site</a>
  </div>
</footer>

<script src="https://cdnjs.cloudflare.com/ajax/libs/tabulator/6.4.0/js/tabulator.min.js"></script>
<script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write `web/styles.css`**

```css
:root {
  --bg: #ffffff; --fg: #1b1f23; --muted: #5c6670; --border: #d0d7de; --accent: #0b5cad;
  --panel: #f6f8fa; --error-bg: #fff1f0; --error-fg: #8a1c1c;
  --cat-s: #2e8b57; --cat-i: #e0a100; --cat-r: #c62828; --cat-none: #c9ced3;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f141a; --fg: #e6edf3; --muted: #9aa5b1; --border: #30363d; --accent: #58a6ff;
          --panel: #161b22; --error-bg: #3a1414; --error-fg: #ffb4b4; --cat-none: #3a434d; }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--fg); background: var(--bg); }
header, main, footer { max-width: 1400px; margin: 0 auto; padding: 0 16px; }
header { padding-top: 16px; }
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { margin: 0; font-size: 1.6rem; }
h2 { font-size: 1.15rem; margin: 24px 0 8px; display: flex; align-items: center; gap: 12px; }
.tagline { color: var(--muted); margin: 6px 0 12px; }
.status { padding: 4px 10px; border-radius: 999px; font-size: 0.9rem; border: 1px solid var(--border); }
.status.loading { background: var(--panel); }
.status.ready { border-color: var(--cat-s); color: var(--cat-s); }
.status.failed { border-color: var(--cat-r); color: var(--cat-r); }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); }
.tabs button { background: none; border: 1px solid transparent; border-bottom: none; padding: 8px 16px; cursor: pointer; color: var(--muted); font-size: 1rem; border-radius: 6px 6px 0 0; }
.tabs button.active { color: var(--fg); border-color: var(--border); background: var(--bg); margin-bottom: -1px; }
.tab { display: none; padding: 16px 0 32px; }
.tab.active { display: block; }
fieldset { border: 1px solid var(--border); border-radius: 6px; margin: 0 0 12px; padding: 10px 14px; background: var(--panel); }
legend { font-weight: 600; padding: 0 6px; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; margin: 6px 0; }
label { display: inline-flex; align-items: center; gap: 6px; }
select, input[type=text], input[type=search] { padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--fg); }
button { padding: 6px 14px; border: 1px solid var(--accent); border-radius: 4px; background: var(--accent); color: #fff; cursor: pointer; font-size: 0.95rem; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
h2 button, #dl-rules { padding: 2px 10px; font-size: 0.85rem; }
.muted { color: var(--muted); font-size: 0.9rem; }
.hidden { display: none !important; }
.error { background: var(--error-bg); color: var(--error-fg); border: 1px solid var(--error-fg); border-radius: 6px; padding: 10px 14px; white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 0.9rem; margin: 12px 0; }
pre#run-log { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px; overflow: auto; max-height: 300px; font-size: 0.85rem; }
.result-block { margin-top: 12px; }
.tabulator { font-size: 13px; border-radius: 6px; }
.rule-link { color: var(--accent); cursor: pointer; text-decoration: underline; }
.matrix-wrap { overflow: auto; max-height: 420px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 12px; }
table.matrix { border-collapse: collapse; font-size: 12px; }
table.matrix th, table.matrix td { border: 1px solid var(--border); padding: 2px 6px; white-space: nowrap; }
table.matrix thead th { position: sticky; top: 0; background: var(--panel); writing-mode: vertical-rl; transform: rotate(180deg); max-height: 160px; vertical-align: bottom; }
table.matrix tbody th { position: sticky; left: 0; background: var(--panel); text-align: left; }
table.matrix td { text-align: center; width: 26px; color: #fff; font-weight: 600; }
.cat-S { background: var(--cat-s); } .cat-I { background: var(--cat-i); } .cat-R { background: var(--cat-r); }
.cat-none { background: var(--cat-none); color: var(--fg) !important; }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: middle; margin: 0 2px 0 8px; }
footer { border-top: 1px solid var(--border); padding: 12px 16px 24px; color: var(--muted); font-size: 0.9rem; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
footer a { color: var(--accent); }
```

- [ ] **Step 3: Write `web/app.js`**

```js
/* AMRrules Interactive: page logic. Talks to worker.js, renders results with Tabulator. */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = {
    manifest: null, rulesIndex: {}, examples: [],
    worker: null, ready: false, runId: 0,
    input: null,            // {name, bytes}
    orgFileText: null,
    pending: Promise.resolve(),
    lastResult: null,
    tables: { summary: null, interpreted: null, rules: null },
    summaryRows: [],
  };
  window.__amrrules = state;   // used by the Playwright parity test

  // ---------- helpers ----------
  function parseTSV(text) {
    // Engine output is written by Python's csv module with tab delimiter; fields never contain tabs or newlines in practice.
    const lines = text.replace(/\r\n/g, "\n").split("\n").filter((l) => l.length > 0);
    if (!lines.length) return { columns: [], rows: [] };
    const columns = lines[0].split("\t");
    const rows = lines.slice(1).map((line) => {
      const cells = line.split("\t");
      const row = {};
      columns.forEach((c, i) => { row[c] = cells[i] === undefined ? "" : cells[i]; });
      return row;
    });
    return { columns, rows };
  }

  function download(filename, text) {
    const blob = new Blob([text], { type: "text/tab-separated-values" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  function setStatus(text, cls) {
    const el = $("#status");
    el.textContent = text;
    el.className = "status " + cls;
  }

  function showError(message) {
    const el = $("#error");
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function fillSelect(select, values, labelFn) {
    values.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = labelFn ? labelFn(v) : v;
      select.appendChild(opt);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- tabs ----------
  function switchTab(name) {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
    Object.values(state.tables).forEach((t) => t && t.redraw(true));
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  // ---------- tables ----------
  function ruleLinkFormatter(cell) {
    const value = cell.getValue();
    if (!value || value === "-") return escapeHtml(value || "");
    const organism = cell.getRow().getData().organism || "";
    return value.split(/[;,]\s*/).map((id) => id.trim()).filter(Boolean)
      .map((id) => `<span class="rule-link" data-rule="${escapeHtml(id)}" data-organism="${escapeHtml(organism)}">${escapeHtml(id)}</span>`)
      .join("; ");
  }

  function makeTable(container, parsed, opts) {
    const ruleCols = new Set(["ruleID", "ruleIDs", "combo rules"]);
    const columns = parsed.columns.map((c) => {
      const col = { title: c, field: c, headerFilter: "input", minWidth: 80 };
      if (ruleCols.has(c)) col.formatter = ruleLinkFormatter;
      return col;
    });
    const table = new Tabulator(container, Object.assign({
      data: parsed.rows, columns, layout: "fitDataTable", height: "420px",
      pagination: false, movableColumns: true, columnDefaults: { tooltip: true },
    }, opts || {}));
    container.addEventListener("click", (ev) => {
      const link = ev.target.closest(".rule-link");
      if (link) showRule(link.dataset.rule, link.dataset.organism);
    });
    return table;
  }

  // ---------- category matrix ----------
  function renderMatrix(rows, level) {
    const wrap = $("#matrix");
    const useClass = level === "class";
    const filtered = rows.filter((r) => useClass ? r.drug === "(all)" : r.drug !== "(all)");
    const colKey = useClass ? "drug class" : "drug";
    const samples = [...new Set(filtered.map((r) => r.sample))];
    const cols = [...new Set(filtered.map((r) => r[colKey]))].sort((a, b) => a.localeCompare(b));
    const lookup = new Map(filtered.map((r) => [r.sample + "\u0000" + r[colKey], r]));
    if (!samples.length || !cols.length) { wrap.innerHTML = '<p class="muted">No rows to display.</p>'; return; }
    let html = '<table class="matrix"><thead><tr><th></th>';
    cols.forEach((c) => { html += `<th title="${escapeHtml(c)}">${escapeHtml(c)}</th>`; });
    html += "</tr></thead><tbody>";
    samples.forEach((s) => {
      html += `<tr><th>${escapeHtml(s)}</th>`;
      cols.forEach((c) => {
        const r = lookup.get(s + "\u0000" + c);
        const cat = r ? r["clinical category"] : "";
        const cls = ["S", "I", "R"].includes(cat) ? "cat-" + cat : "cat-none";
        const tip = r ? `${escapeHtml(c)}\ncategory: ${escapeHtml(cat)}\nphenotype: ${escapeHtml(r.phenotype)}\nevidence: ${escapeHtml(r["evidence grade"])}\nnon-S markers: ${escapeHtml(r["markers (non-S)"])}\nrules: ${escapeHtml(r.ruleIDs)}` : "no row";
        html += `<td class="${cls}" title="${tip}">${["S", "I", "R"].includes(cat) ? cat : ""}</td>`;
      });
      html += "</tr>";
    });
    wrap.innerHTML = html + "</tbody></table>";
  }
  $("#matrix-level").addEventListener("change", (e) => renderMatrix(state.summaryRows, e.target.value));

  // ---------- results ----------
  function renderResult(result) {
    state.lastResult = result;
    $("#run-log").textContent = result.log || "";
    if (!result.ok) {
      showError((result.error || "AMRrules failed") + (result.log ? "\n\n--- log ---\n" + result.log : ""));
      $("#results").classList.add("hidden");
      return;
    }
    $("#error").classList.add("hidden");
    $("#results").classList.remove("hidden");
    const summary = parseTSV(result.summary);
    const interpreted = parseTSV(result.interpreted);
    state.summaryRows = summary.rows;
    if (state.tables.summary) state.tables.summary.destroy();
    if (state.tables.interpreted) state.tables.interpreted.destroy();
    state.tables.summary = makeTable($("#table-summary"), summary);
    state.tables.interpreted = makeTable($("#table-interpreted"), interpreted);
    renderMatrix(summary.rows, $("#matrix-level").value);
    $("#dl-summary").onclick = () => download(`${result.prefix}_genome_summary.tsv`, result.summary);
    $("#dl-interpreted").onclick = () => download(`${result.prefix}_interpreted.tsv`, result.interpreted);
  }

  // ---------- inputs ----------
  function readFileBytes(file) {
    return file.arrayBuffer().then((buf) => new Uint8Array(buf));
  }

  $("#input-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("#example-input").value = "";
    state.pending = readFileBytes(file).then((bytes) => {
      state.input = { name: file.name, bytes };
      $("#input-name").textContent = `${file.name} (${(bytes.length / 1024).toFixed(1)} kB)`;
    });
  });

  $("#example-input").addEventListener("change", (e) => {
    const name = e.target.value;
    if (!name) return;
    $("#input-file").value = "";
    state.pending = fetch("build/examples/" + name).then((r) => r.arrayBuffer()).then((buf) => {
      state.input = { name, bytes: new Uint8Array(buf) };
      $("#input-name").textContent = `example: ${name}`;
    });
  });

  $("#organism-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("#example-orgfile").value = "";
    document.querySelector('input[name=mode][value=multi]').checked = true;
    state.pending = file.text().then((text) => {
      state.orgFileText = text;
      $("#orgfile-name").textContent = file.name;
    });
  });

  $("#example-orgfile").addEventListener("change", (e) => {
    const name = e.target.value;
    if (!name) return;
    $("#organism-file").value = "";
    document.querySelector('input[name=mode][value=multi]').checked = true;
    state.pending = fetch("build/examples/" + name).then((r) => r.text()).then((text) => {
      state.orgFileText = text;
      $("#orgfile-name").textContent = `example: ${name}`;
    });
  });

  // ---------- run ----------
  $("#run-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#error").classList.add("hidden");
    await state.pending;
    if (!state.input) { showError("Choose an AMRFinderPlus output file or an example first."); return; }
    const multi = document.querySelector('input[name=mode]:checked').value === "multi";
    if (multi && state.orgFileText == null) { showError("Organism-file mode needs an organism file."); return; }
    const opts = {
      organism: multi ? null : $("#organism").value,
      output_prefix: $("#opt-prefix").value.trim() || "amrrules",
      sample_id: multi ? null : ($("#opt-sample-id").value.trim() || null),
      no_rule_interpretation: $("#opt-nr").value,
      annot_opts: $("#opt-annot").value,
      flag_core: $("#opt-flag-core").checked,
      full_disrupt: $("#opt-full-disrupt").checked,
      print_non_amr: $("#opt-non-amr").checked,
    };
    state.lastResult = null;
    $("#run-btn").disabled = true;
    $("#run-status").textContent = "Running…";
    state.worker.postMessage({
      type: "run", id: ++state.runId, opts,
      inputName: state.input.name, inputBytes: state.input.bytes,
      organismFileText: multi ? state.orgFileText : null,
    });
  });

  // ---------- rules browser ----------
  async function loadRules(organism) {
    const file = state.rulesIndex[organism];
    if (!file) return;
    const text = await fetch("build/rules/" + file).then((r) => r.text());
    const parsed = parseTSV(text);
    if (state.tables.rules) state.tables.rules.destroy();
    state.tables.rules = new Tabulator($("#table-rules"), {
      data: parsed.rows, layout: "fitDataTable", height: "600px", movableColumns: true,
      columnDefaults: { tooltip: true, headerFilter: "input", minWidth: 80 },
      columns: parsed.columns.map((c) => {
        const col = { title: c, field: c };
        if (c === "PMID") col.formatter = (cell) => (cell.getValue() || "").split(/[;,]\s*/).filter(Boolean)
          .map((p) => /^\d+$/.test(p) ? `<a href="https://pubmed.ncbi.nlm.nih.gov/${p}/" target="_blank" rel="noopener">${p}</a>` : escapeHtml(p)).join("; ");
        return col;
      }),
    });
    state.tables.rules.on("dataFiltered", (filters, rows) => { $("#rules-count").textContent = `${rows.length} / ${parsed.rows.length} rules`; });
    state.tables.rules.on("tableBuilt", () => { $("#rules-count").textContent = `${parsed.rows.length} rules`; applyRulesSearch(); });
    $("#dl-rules").onclick = () => download(file, text);
  }

  function applyRulesSearch() {
    const table = state.tables.rules;
    if (!table) return;
    const q = $("#rules-search").value.trim().toLowerCase();
    if (!q) { table.clearFilter(true); return; }
    table.setFilter((row) => Object.values(row).some((v) => String(v).toLowerCase().includes(q)));
  }
  $("#rules-search").addEventListener("input", applyRulesSearch);
  $("#rules-organism").addEventListener("change", (e) => loadRules(e.target.value));

  async function showRule(ruleId, organism) {
    switchTab("rules");
    const select = $("#rules-organism");
    if (organism && state.rulesIndex[organism] && select.value !== organism) {
      select.value = organism;
      await loadRules(organism);
    }
    $("#rules-search").value = ruleId;
    applyRulesSearch();
  }

  // ---------- worker ----------
  function startWorker() {
    const worker = new Worker("worker.js");
    state.worker = worker;
    worker.onmessage = (ev) => {
      const msg = ev.data;
      if (msg.type === "phase") {
        setStatus(msg.text + "…", "loading");
        $("#run-status").textContent = msg.text + "…";
      } else if (msg.type === "ready") {
        state.ready = true;
        setStatus(`Ready · amrrules ${msg.info.version}`, "ready");
        $("#run-status").textContent = "";
        $("#run-btn").disabled = false;
      } else if (msg.type === "result") {
        $("#run-btn").disabled = false;
        $("#run-status").textContent = "";
        renderResult(msg.result);
      } else if (msg.type === "error") {
        $("#run-btn").disabled = !state.ready;
        $("#run-status").textContent = "";
        if (!state.ready) setStatus("Failed to start Python runtime", "failed");
        state.lastResult = { ok: false, error: msg.message };
        showError(msg.message);
      }
    };
    worker.onerror = (ev) => {
      setStatus("Worker error", "failed");
      showError("Worker error: " + (ev.message || "unknown"));
    };
    worker.postMessage({ type: "init", baseUrl: document.baseURI, manifest: state.manifest });
  }

  // ---------- boot ----------
  async function boot() {
    try {
      const [manifest, rulesIndex, examples] = await Promise.all([
        fetch("build/manifest.json").then((r) => r.json()),
        fetch("build/rules_index.json").then((r) => r.json()),
        fetch("build/examples_index.json").then((r) => r.json()),
      ]);
      state.manifest = manifest; state.rulesIndex = rulesIndex; state.examples = examples;
      const organisms = Object.keys(rulesIndex);
      fillSelect($("#organism"), organisms);
      fillSelect($("#rules-organism"), organisms);
      fillSelect($("#example-input"), examples.filter((n) => !/species/.test(n)));
      fillSelect($("#example-orgfile"), examples.filter((n) => /species/.test(n)));
      $("#versions").textContent = `amrrules ${manifest.amrrules_version} · AMRFinderPlus DB ${manifest.amrfp_db_version} · CARD ${manifest.card_version} · built ${manifest.built_at} (${manifest.git_commit})`;
      const defaultOrg = organisms.includes("s__Escherichia coli") ? "s__Escherichia coli" : organisms[0];
      $("#rules-organism").value = defaultOrg;
      loadRules(defaultOrg);
      startWorker();
    } catch (err) {
      setStatus("Failed to load site data", "failed");
      showError("Could not load build/manifest.json. Run `python scripts/build_web.py` first.\n" + err);
    }
  }
  boot();
})();
```

- [ ] **Step 4: Create `web/.nojekyll`** (empty file): `touch web/.nojekyll`

- [ ] **Step 5: Smoke test in a browser**

Run: `conda run -n amrrules python -m http.server -d web 8765 &` then, with Playwright in Python:

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()
    page.goto("http://127.0.0.1:8765/"); page.wait_for_selector("#status.ready", timeout=180000)
    page.select_option("#example-input", "test_ecoli_wildtype.tsv"); page.select_option("#organism", "s__Escherichia coli")
    page.click("#run-btn"); page.wait_for_function("window.__amrrules.lastResult !== null", timeout=120000)
    r = page.evaluate("window.__amrrules.lastResult"); print(r["ok"], r["log"][-200:])
    page.screenshot(path="/tmp/claude-1000/-home-analysis-Desktop/61a99c2c-8533-47eb-8165-05806a591f09/scratchpad/smoke.png", full_page=True); b.close()
```
Expected: `True` and `AMRrules complete.` in the log; screenshot shows matrix and two tables. Fix any console errors before continuing.

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/styles.css web/app.js web/.nojekyll
git commit -m "web: add interactive page with results tables, category matrix and rule browser"
```

---

### Task 4: Playwright parity test

**Files:**
- Create: `tests/web/test_parity.py`

- [ ] **Step 1: Write the test**

```python
"""Browser output must equal CLI output byte-for-byte for a set of representative runs.

Marked integration: needs the built site (web/build), Playwright Chromium, and
network access for the Pyodide CDN.
"""
import functools
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
INPUT = ROOT / "tests" / "data" / "input"

# name, input file, organism, organism file, extra CLI args, UI options
CASES = [
    ("ecoli_wt", "test_ecoli_wildtype.tsv", "s__Escherichia coli", None, [], {}),
    ("kp_wt_core", "test_kpneumo_wildtype.tsv", "s__Klebsiella pneumoniae", None, ["--flag-core"], {"flag_core": True}),
    ("kp_mdr_nwtR", "test_kpneumo_MDR.tsv", "s__Klebsiella pneumoniae", None, ["-nr", "nwtR"], {"nr": "nwtR"}),
    ("kp_mdr_nwtS_full", "test_kpneumo_MDR.tsv", "s__Klebsiella pneumoniae", None, ["-nr", "nwtS", "-a", "full"], {"nr": "nwtS", "annot": "full"}),
    ("kp_disrupt_full", "test_kpneumo_disrupt.tsv", "s__Klebsiella pneumoniae", None, ["--full-disrupt"], {"full_disrupt": True}),
    ("multispp", "test_multispp_amrfp.tsv", None, "test_multispp_species.tsv", [], {}),
    ("bordetella", "test_bordetella_20strains.tsv", None, "test_bordetella_species.tsv", [], {}),
    ("ecoli20_nwt", "test_ecoli_20strains.tsv", "s__Escherichia coli", None, ["-nr", "nwt"], {"nr": "nwt"}),
    ("saureus20", "test_saureus_20strains.tsv", "s__Staphylococcus aureus", None, [], {}),
    ("mtb20_nonamr", "test_mycotb_20strains.tsv", "s__Mycobacterium tuberculosis", None, ["--print-non-amr"], {"print_non_amr": True}),
]


@pytest.fixture(scope="session")
def site_url():
    if not (WEB / "build" / "manifest.json").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_web.py"), "--skip-download"], check=True, cwd=ROOT)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(scope="session")
def page(site_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(site_url)
        page.wait_for_selector("#status.ready", timeout=240_000)
        assert not errors, errors
        yield page
        browser.close()


def run_cli(name, inp, organism, orgfile, extra, out_dir):
    cmd = [sys.executable, "-m", "amrrules", "--input", str(INPUT / inp), "--output-prefix", name, "--output-dir", str(out_dir)]
    cmd += ["--organism-file", str(INPUT / orgfile)] if orgfile else ["--organism", organism]
    subprocess.run(cmd + extra, check=True, cwd=ROOT, capture_output=True)
    return (out_dir / f"{name}_interpreted.tsv").read_bytes().decode(), (out_dir / f"{name}_genome_summary.tsv").read_bytes().decode()


def run_browser(page, name, inp, organism, orgfile, opts):
    page.set_input_files("#input-file", str(INPUT / inp))
    if orgfile:
        page.set_input_files("#organism-file", str(INPUT / orgfile))
        page.check("input[name=mode][value=multi]")
    else:
        page.check("input[name=mode][value=single]")
        page.select_option("#organism", organism)
    page.select_option("#opt-nr", opts.get("nr", "none"))
    page.select_option("#opt-annot", opts.get("annot", "minimal"))
    page.set_checked("#opt-flag-core", bool(opts.get("flag_core")))
    page.set_checked("#opt-full-disrupt", bool(opts.get("full_disrupt")))
    page.set_checked("#opt-non-amr", bool(opts.get("print_non_amr")))
    page.fill("#opt-prefix", name)
    page.fill("#opt-sample-id", "")
    page.click("#run-btn")
    page.wait_for_function("window.__amrrules.lastResult !== null", timeout=180_000)
    return page.evaluate("window.__amrrules.lastResult")


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_browser_matches_cli(page, tmp_path, case):
    name, inp, organism, orgfile, extra, opts = case
    cli_interp, cli_summary = run_cli(name, inp, organism, orgfile, extra, tmp_path)
    result = run_browser(page, name, inp, organism, orgfile, opts)
    assert result["ok"], result.get("error") or result.get("log")
    assert result["summary"] == cli_summary
    assert result["interpreted"] == cli_interp
    assert result["prefix"] == name


def test_rule_link_opens_rule_browser(page):
    result = run_browser(page, "link", "test_ecoli_wildtype.tsv", "s__Escherichia coli", None, {})
    assert result["ok"]
    page.click("#table-summary .rule-link >> nth=0")
    page.wait_for_selector("#tab-rules.active")
    page.wait_for_function("document.querySelectorAll('#table-rules .tabulator-row').length >= 1", timeout=30_000)
    assert page.input_value("#rules-organism") == "s__Escherichia coli"
    assert page.input_value("#rules-search").startswith("ECO")


def test_bad_input_shows_error(page, tmp_path):
    bad = tmp_path / "bad.tsv"
    bad.write_text("this\tis\tnot\tamrfinderplus\n")
    page.set_input_files("#input-file", str(bad))
    page.check("input[name=mode][value=single]")
    page.select_option("#organism", "s__Escherichia coli")
    page.click("#run-btn")
    page.wait_for_function("window.__amrrules.lastResult !== null", timeout=60_000)
    result = page.evaluate("window.__amrrules.lastResult")
    assert result["ok"] is False
    assert not page.locator("#error").evaluate("el => el.classList.contains('hidden')")
```

- [ ] **Step 2: Run the test**

Run: `conda run -n amrrules pytest tests/web/test_parity.py -v -x`
Expected: all cases pass. If a case differs, print both strings' first differing line and fix `runner.py`/`app.js` (never the engine).

- [ ] **Step 3: Commit**

```bash
git add tests/web/test_parity.py
git commit -m "test: browser vs CLI parity test with Playwright"
```

---

### Task 5: CI/CD workflow, README

**Files:**
- Create: `.github/workflows/deploy-pages.yml`
- Modify: `README.md`

- [ ] **Step 1: Write the workflow**

```yaml
name: Build and deploy interactive site

on:
  push:
    branches: [main]
  schedule:
    - cron: "0 3 * * 1"   # weekly: pick up new AMRFinderPlus / CARD resource releases
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install package and dev tools
        run: |
          python -m pip install --upgrade pip
          python copy_rules.py
          pip install -e . build pytest playwright
          python -m playwright install --with-deps chromium
      - name: Build web assets (downloads AMRFinderPlus and CARD resources)
        run: python scripts/build_web.py
      - name: Test build outputs and glue
        run: pytest tests/web/test_build.py -v
      - name: Browser vs CLI parity
        run: pytest tests/web/test_parity.py -v
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: web

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 2: README section** (insert after the Zenodo DOI line)

```markdown
## Interactive browser version

This fork adds a client-side web app, deployed to GitHub Pages at
**https://cinnetcrash.github.io/AMRrules_Interactive/**. It runs the unmodified
`amrrules` engine in the browser via [Pyodide](https://pyodide.org/): upload an
AMRFinderPlus output file, choose an organism (or an organism file for multi-sample
input), set the same options as the CLI, and view/download the interpreted genotype
report and genome summary report. A second tab browses the rule files. Uploaded files
never leave your machine. Browser output is checked against CLI output byte-for-byte
in CI (`tests/web/test_parity.py`).

Local preview: `make web && make serve-web`, then open http://localhost:8000/.
```

- [ ] **Step 3: Validate the workflow YAML**

Run: `conda run -n amrrules python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy-pages.yml')); print('yaml ok')"` (install pyyaml if missing).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy-pages.yml README.md
git commit -m "ci: build, test and deploy the interactive site to GitHub Pages"
```

---

### Task 6: Enable Pages, push, verify deployment

- [ ] **Step 1: Enable GitHub Pages with the Actions source**

Run: `gh api -X POST repos/cinnetcrash/AMRrules_Interactive/pages -f build_type=workflow`
Expected: JSON with `"build_type": "workflow"`. (A 409 means it already exists.)

- [ ] **Step 2: Push**

Run: `git push origin main`

- [ ] **Step 3: Watch the workflow**

Run: `gh run watch --repo cinnetcrash/AMRrules_Interactive $(gh run list --repo cinnetcrash/AMRrules_Interactive --workflow "Build and deploy interactive site" --limit 1 --json databaseId -q '.[0].databaseId') --exit-status`
Expected: success. On failure, `gh run view <id> --log-failed`, fix, commit, push again.

- [ ] **Step 4: Verify the live site**

Run the Task 3 Step 5 Playwright smoke script against `https://cinnetcrash.github.io/AMRrules_Interactive/`.
Expected: `True` and `AMRrules complete.`

---

## Self-review

- Spec coverage: build step (T1), runtime/worker/stub/patch (T2), UI with options, tables, matrix, downloads, rule browser, ruleID links, header/footer versions and privacy line (T3), error handling for engine errors and CDN failures (T2 worker catch + T3 `error` branch), parity and build tests (T2, T4), workflow with weekly schedule and Pages (T5, T6), upstream sync untouched (no engine files modified).
- Names used consistently: `state.lastResult`, `#status.ready`, `build/manifest.json` keys (`wheel`, `amrrules_version`, `amrfp_db_version`, `card_version`, `built_at`, `git_commit`), runner functions `setup(resources)` / `run(opts_json, input_name, input_bytes, organism_file_text)`, worker messages `init/run/phase/ready/result/error`.
