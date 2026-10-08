"""Checks that scripts/build_web.py produces every asset the browser UI needs,
and that the Python glue used in the browser (web/py/runner.py) behaves like the CLI."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "web" / "build"
PKG_RES = ROOT / "src" / "amrrules" / "resources"
DOWNLOADED = ["aro.obo", "aro_categories.tsv", "ReferenceGeneHierarchy.txt", "version.txt"]
RESOURCE_NAMES = ["ReferenceGeneHierarchy.txt", "version.txt", "amrfp_to_card_drugs_classes.txt", "card_drug_class_map.json"]


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


def _load_runner(tmp_path):
    spec = importlib.util.spec_from_file_location("runner", ROOT / "web" / "py" / "runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.WORK = tmp_path / "work"
    runner.IN = runner.WORK / "in"
    runner.OUT = runner.WORK / "out"
    return runner


def _resources(built):
    # bytes->str without newline translation, exactly like the browser's fetch().text()
    return {name: (built / "resources" / name).read_bytes().decode("utf-8") for name in RESOURCE_NAMES}


def test_runner_glue_matches_cli(built, tmp_path):
    """runner.py is the code that runs in the browser; exercise it on CPython too."""
    runner = _load_runner(tmp_path)
    info = json.loads(runner.setup(_resources(built)))
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
    runner = _load_runner(tmp_path)
    runner.setup(_resources(built))
    result = json.loads(runner.run(json.dumps({"organism": "s__Escherichia coli"}), "bad.tsv", b"not\ta\tvalid\tfile\n", None))
    assert result["ok"] is False
    assert result["error"]
