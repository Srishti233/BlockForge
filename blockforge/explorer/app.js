"use strict";
// BlockForge explorer. No external assets. Every value rendered into HTML goes through esc().
const $app = document.getElementById("app");
let timer = null;

function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
const short = (h) => (h ? esc(String(h).slice(0, 12)) + "&hellip;" : "");
const when = (t) => esc(new Date(Number(t) * 1000).toLocaleString());
async function api(path, opts) {
  const r = await fetch("/api/v1" + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((body.error && body.error.message) || r.statusText);
  return body;
}
const blockLink = (h, label) => `<a href="#/block/${esc(h)}">${label === undefined ? short(h) : label}</a>`;
const txLink = (h) => `<a href="#/tx/${esc(h)}">${short(h)}</a>`;
const addrLink = (a) => (/^bf[0-9a-f]{40}$/.test(a) ? `<a href="#/address/${esc(a)}">${esc(a.slice(0, 12))}&hellip;</a>` : esc(a));
const card = (k, v) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;

function setTimer(fn, ms) { clearInterval(timer); timer = ms ? setInterval(fn, ms) : null; }
function fail(e) { $app.innerHTML = `<p class="error">${esc(e.message || e)}</p>`; }

// ------------------------------------------------------------------ pages
async function dashboard() {
  async function render() {
    const [info, node, mem, peers, blocks] = await Promise.all([
      api("/chain/info"), api("/node"), api("/mempool"), api("/peers"), api("/blocks?limit=8")]);
    const latest = blocks.blocks[0];
    $app.innerHTML = `<h1>Dashboard</h1><div class="grid">
      ${card("Height", esc(info.height))}${card("Difficulty (bits)", esc(info.difficulty_bits))}
      ${card("Pending txs", esc(mem.count))}${card("Peers", esc(peers.peers.length))}
      ${card("Status", node.mining ? '<span class="ok">mining</span>' : "idle")}
      ${card("Chain", esc(info.chain_id))}</div>
      <h2>Tip</h2><p class="mono">${esc(info.tip_hash)}</p>
      <p class="muted">State root <span class="mono">${esc(info.state_root)}</span></p>
      <h2>Latest blocks</h2>${blocksTable(blocks.blocks)}
      ${latest ? "" : "<p class='muted'>No blocks yet.</p>"}`;
  }
  await render().catch(fail);
  setTimer(() => render().catch(fail), 3000);
}
function blocksTable(list) {
  return `<div class="scroll"><table><tr><th>Height</th><th>Hash</th><th>Time</th><th>Txs</th><th>Miner</th></tr>${
    list.map((b) => `<tr><td>${blockLink(b.height, esc(b.height))}</td><td class="mono">${blockLink(b.hash)}</td>
      <td>${when(b.timestamp)}</td><td>${esc(b.tx_count)}</td><td class="mono">${addrLink(b.miner)}</td></tr>`).join("")}</table></div>`;
}
async function blocksPage(page) {
  const per = 20, offset = page * per;
  const d = await api(`/blocks?limit=${per}&offset=${offset}`);
  const pages = Math.max(1, Math.ceil(d.total / per));
  $app.innerHTML = `<h1>Blocks <span class="muted">(${esc(d.total)})</span></h1>${blocksTable(d.blocks)}
    <p>${page > 0 ? `<a href="#/blocks/${page - 1}">&larr; newer</a>` : ""}
    ${page + 1 < pages ? ` <a href="#/blocks/${page + 1}">older &rarr;</a>` : ""}</p>`;
  setTimer(null);
}
function txTable(txs) {
  return `<div class="scroll"><table><tr><th>Tx</th><th>From</th><th>To</th><th>Amount</th><th>Fee</th></tr>${
    txs.map((t) => `<tr><td class="mono">${txLink(t.tx_id)}</td><td class="mono">${addrLink(t.sender)}</td>
      <td class="mono">${addrLink(t.recipient)}</td><td>${esc(t.amount)}</td><td>${esc(t.fee)}</td></tr>`).join("")}</table></div>`;
}
async function blockPage(id) {
  const b = await api("/blocks/" + encodeURIComponent(id));
  const row = (k, v) => `<tr><th>${esc(k)}</th><td class="mono">${v}</td></tr>`;
  $app.innerHTML = `<h1>Block ${esc(b.height)} ${b.is_main ? "" : '<span class="bad">(side branch)</span>'}</h1>
    <table>${row("Hash", esc(b.hash))}${row("Previous", blockLink(b.prev_hash, esc(b.prev_hash)))}
    ${row("Merkle root", esc(b.merkle_root))}${row("State root", esc(b.state_root))}
    ${row("Time", when(b.timestamp))}${row("Difficulty bits", esc(b.difficulty_bits))}${row("Nonce", esc(b.nonce))}
    ${row("Miner", addrLink(b.miner))}${row("Size", esc(b.size_bytes) + " bytes")}${row("Confirmations", esc(b.confirmations))}</table>
    <h2>Transactions (${esc(b.transactions.length)})</h2>${txTable(b.transactions)}`;
  setTimer(null);
}
async function txPage(id) {
  const t = await api("/transactions/" + encodeURIComponent(id));
  const x = t.transaction;
  const row = (k, v) => `<tr><th>${esc(k)}</th><td class="mono">${v}</td></tr>`;
  $app.innerHTML = `<h1>Transaction</h1><table>
    ${row("Tx id", esc(x.tx_id))}${row("Status", t.status === "confirmed" ? '<span class="ok">confirmed</span>' : "pending")}
    ${row("From", addrLink(x.sender))}${row("To", addrLink(x.recipient))}${row("Amount", esc(x.amount))}${row("Fee", esc(x.fee))}
    ${row("Nonce", esc(x.nonce))}${row("Chain id", esc(x.chain_id))}${row("Signature", esc(x.signature))}
    ${t.block_hash ? row("Block", blockLink(t.block_hash, esc(t.block_height)) + " &nbsp; confirmations: " + esc(t.confirmations)) : ""}
    ${t.merkle_root ? row("Merkle root", esc(t.merkle_root)) : ""}</table>
    ${t.merkle_proof ? `<h2>Merkle proof</h2><button id="verify">Verify Merkle Proof</button>
      <div id="vout" aria-live="polite"></div>` : "<p class='muted'>Not yet in a block, so no Merkle proof.</p>"}`;
  setTimer(null);
  if (t.merkle_proof) document.getElementById("verify").addEventListener("click", () => verifyProof(x.tx_id, t));
}
// ---- Merkle verification: server answer + local recomputation, step by step
const hexToBytes = (h) => new Uint8Array(h.match(/../g).map((b) => parseInt(b, 16)));
const bytesToHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
async function sha(bytes) { return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)); }
function concat(...parts) { const o = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let i = 0; for (const p of parts) { o.set(p, i); i += p.length; } return o; }
async function verifyProof(txId, t) {
  const out = document.getElementById("vout");
  out.innerHTML = "<p>Verifying&hellip;</p>";
  let html = "";
  try {
    const server = await api("/merkle/verify", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tx_id: txId, proof: t.merkle_proof, root: t.merkle_root }) });
    html += `<p>Server says: ${server.valid ? '<span class="ok">valid</span>' : '<span class="bad">INVALID</span>'}</p>`;
    if (!window.crypto || !crypto.subtle) throw new Error("this browser context has no WebCrypto (use http://127.0.0.1 or localhost)");
    let cur = await sha(concat(new Uint8Array([0]), hexToBytes(txId)));
    html += `<div class="step">leaf = SHA-256(0x00 &#8214; txid) = <code>${esc(bytesToHex(cur))}</code></div>`;
    let n = 0;
    for (const s of t.merkle_proof) {
      const sib = hexToBytes(s.hash);
      cur = await sha(s.side === "left" ? concat(new Uint8Array([1]), sib, cur) : concat(new Uint8Array([1]), cur, sib));
      n += 1;
      html += `<div class="step">level ${n}: sibling on the <b>${esc(s.side)}</b> <code>${esc(s.hash.slice(0, 16))}&hellip;</code>
        &rarr; <code>${esc(bytesToHex(cur))}</code></div>`;
    }
    const ok = bytesToHex(cur) === t.merkle_root;
    html += `<div class="step ${ok ? "good" : "fail"}">computed root <code>${esc(bytesToHex(cur))}</code><br>
      block root &nbsp;&nbsp;&nbsp;<code>${esc(t.merkle_root)}</code><br>
      ${ok ? '<span class="ok">Match: the transaction is in this block.</span>' : '<span class="bad">Mismatch.</span>'}</div>`;
  } catch (e) { html += `<p class="error">${esc(e.message)}</p>`; }
  out.innerHTML = html;
}
async function addressPage(a) {
  const acct = await api("/accounts/" + encodeURIComponent(a));
  $app.innerHTML = `<h1>Account</h1><p class="mono">${esc(acct.address)}</p><div class="grid">
    ${card("Balance", esc(acct.balance))}${card("Confirmed nonce", esc(acct.nonce))}
    ${card("Next nonce", esc(acct.next_nonce))}${card("Pending txs", esc(acct.pending_transactions))}</div>`;
  setTimer(null);
}
async function networkPage() {
  async function render() {
    const [p, f, info] = await Promise.all([api("/peers"), api("/chain/forks"), api("/chain/info")]);
    $app.innerHTML = `<h1>Network</h1><p class="muted">This node: <span class="mono">${esc(info.node_id)}</span></p>
      <h2>Peers (${esc(p.peers.length)})</h2><div class="scroll"><table><tr><th>Address</th><th>Node id</th><th>Height</th><th>Score</th><th>Failures</th></tr>${
      p.peers.map((x) => `<tr><td class="mono">${esc(x.addr)}</td><td class="mono">${esc(x.node_id)}</td><td>${esc(x.tip_height)}</td><td>${esc(x.score)}</td><td>${esc(x.failures)}</td></tr>`).join("")}</table></div>
      <h2>Fork events (${esc(f.forks.length)})</h2><div class="scroll"><table><tr><th>Time</th><th>Ancestor</th><th>Depth</th><th>New branch</th><th>Old tip</th><th>New tip</th><th>Txs returned</th></tr>${
      f.forks.slice().reverse().map((x) => `<tr><td>${when(x.time)}</td><td>${esc(x.ancestor_height)}</td><td>${esc(x.depth)}</td><td>${esc(x.new_branch_length)}</td>
        <td class="mono">${short(x.old_tip)}</td><td class="mono">${blockLink(x.new_tip)}</td><td>${esc(x.orphaned_txs_returned)}</td></tr>`).join("")}</table></div>`;
  }
  await render().catch(fail);
  setTimer(() => render().catch(fail), 4000);
}
// ------------------------------------------------------------------ routing & search
function route() {
  const [, page, arg] = (location.hash || "#/").split("/");
  const run = { "": dashboard, blocks: () => blocksPage(Number(arg) || 0), block: () => blockPage(arg),
    tx: () => txPage(arg), address: () => addressPage(arg), network: networkPage }[page || ""];
  if (!run) return fail("Unknown page");
  Promise.resolve(run()).catch(fail);
}
document.getElementById("search").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = document.getElementById("q").value.trim().toLowerCase();
  if (/^[0-9]+$/.test(q)) location.hash = "#/block/" + q;
  else if (/^bf[0-9a-f]{40}$/.test(q)) location.hash = "#/address/" + q;
  else if (/^[0-9a-f]{64}$/.test(q)) {
    api("/blocks/" + q).then(() => { location.hash = "#/block/" + q; }, () => { location.hash = "#/tx/" + q; });
  } else fail("Enter a block height, 64-hex block hash or tx id, or a bf... address.");
});
window.addEventListener("hashchange", route);
route();
