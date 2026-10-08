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
        target = res_dir / name
        data = resources[name].encode("utf-8")
        # On CPython with an editable install this directory is the source tree:
        # only write when the content actually differs, so tests never rewrite repo files.
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)

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
