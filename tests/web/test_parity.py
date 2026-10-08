"""Browser output must equal CLI output byte-for-byte for a set of representative runs.

Marked integration: needs the built site (web/build), Playwright Chromium, and
network access for the Pyodide CDN.
"""
import functools
import http.server
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
    handler.log_message = lambda *a, **k: None
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
    page.click(".tabs button[data-tab=interpret]")


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
