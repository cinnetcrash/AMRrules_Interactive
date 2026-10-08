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
