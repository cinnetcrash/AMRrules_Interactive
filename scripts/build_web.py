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
