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
