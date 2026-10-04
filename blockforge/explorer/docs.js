"use strict";
function esc(v) { return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
fetch("/openapi.json").then((r) => r.json()).then((spec) => {
  const schemas = (spec.components && spec.components.schemas) || {};
  const ref = (s) => (s && s.$ref ? schemas[s.$ref.split("/").pop()] : s);
  let html = `<h1>${esc(spec.info.title)} <span class="muted">v${esc(spec.info.version)}</span></h1><p>${esc(spec.info.description || "")}</p>`;
  for (const [path, ops] of Object.entries(spec.paths)) {
    for (const [method, op] of Object.entries(ops)) {
      const params = (op.parameters || []).map((p) => `<li><code>${esc(p.name)}</code> (${esc(p.in)}${p.required ? ", required" : ""})</li>`).join("");
      const body = op.requestBody && ref(op.requestBody.content["application/json"].schema);
      html += `<div class="card endpoint"><b>${esc(method.toUpperCase())}</b> <code>${esc(path)}</code>
        <div class="muted">${esc(op.summary || "")} ${esc((op.tags || []).join(", "))}</div>
        ${params ? `<ul>${params}</ul>` : ""}${body ? `<details><summary>request body</summary><pre class="mono">${esc(JSON.stringify(body.properties || body, null, 1))}</pre></details>` : ""}</div>`;
    }
  }
  document.getElementById("docs").innerHTML = html;
}).catch((e) => { document.getElementById("docs").textContent = "Failed to load spec: " + e.message; });
