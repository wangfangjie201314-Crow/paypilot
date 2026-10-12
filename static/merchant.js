// PayPilot merchant dashboard (private page).
//
// The operator pastes MERCHANT_API_TOKEN into the input; it lives in the
// `merchantToken` variable ONLY — never in HTML, never in localStorage,
// never in cookies. Closing the tab forgets it.
//
// All rendering uses DOM APIs (textContent / property assignment), never
// innerHTML, so order data cannot become markup.
let merchantToken = "";

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function mfetch(url, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  headers["Authorization"] = "Bearer " + merchantToken;
  opts.headers = headers;
  return fetch(url, opts);
}

function loadOrders() {
  mfetch("/api/merchant/orders").then(r => {
    if (r.status === 401) throw new Error("bad-token");
    return r.json();
  }).then(rows => {
    const tb = document.querySelector("#orders tbody");
    while (tb.firstChild) tb.removeChild(tb.firstChild);
    rows.forEach(o => {
      const tr = document.createElement("tr");
      const tdId = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = String(o.paypal_order_id).slice(0, 10) + "…";
      tdId.appendChild(code); tr.appendChild(tdId);
      const tdItems = document.createElement("td");
      tdItems.textContent = o.items.map(i => i.quantity + "× " + i.name).join(", ");
      tr.appendChild(tdItems);
      const tdAmt = document.createElement("td");
      tdAmt.textContent = "$" + (o.amount_cents / 100).toFixed(2);
      tr.appendChild(tdAmt);
      const tdSt = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = "pill " + o.status;
      pill.textContent = o.status;
      tdSt.appendChild(pill); tr.appendChild(tdSt);
      const tdAct = document.createElement("td");
      if (o.status === "CREATED") {
        // No inline onclick: the order id never enters an HTML string.
        const btn = document.createElement("button");
        btn.className = "linkbtn";
        btn.textContent = "Draft follow-up";
        btn.addEventListener("click", () => draftFollowup(o.paypal_order_id));
        tdAct.appendChild(btn);
      }
      tr.appendChild(tdAct);
      tb.appendChild(tr);
    });
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5; td.className = "muted";
      td.textContent = "No orders yet — go chat as a buyer first!";
      tr.appendChild(td); tb.appendChild(tr);
    }
  }).catch(e => {
    if (e.message === "bad-token") {
      setText("gate-msg", "❌ Token invalid — check the server console and try again.");
      showGate();
    }
  });
}

function genBrief() {
  setText("brief", "Generating…");
  mfetch("/api/merchant/brief", {method: "POST"}).then(r => {
    if (r.status === 401) throw new Error("bad-token");
    return r.json();
  }).then(d => setText("brief", d.brief))
   .catch(() => setText("brief", "Failed — is your token still valid?"));
}

function draftFollowup(orderId) {
  setText("followup", "Drafting…");
  mfetch("/api/merchant/followup", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({order_id: orderId})}).then(r => r.json())
    .then(d => setText("followup", d.draft));
}

function showGate() {
  document.getElementById("gate").hidden = false;
  document.getElementById("dash").hidden = true;
}
function showDash() {
  document.getElementById("gate").hidden = true;
  document.getElementById("dash").hidden = false;
  loadOrders();
}

document.getElementById("connect").addEventListener("click", () => {
  merchantToken = document.getElementById("token").value.trim();
  document.getElementById("token").value = "";  // don't leave it in the DOM
  if (!merchantToken) {
    setText("gate-msg", "Paste the token first.");
    return;
  }
  mfetch("/api/merchant/orders").then(r => {
    if (r.status === 401) {
      merchantToken = "";
      setText("gate-msg", "❌ Token invalid — check the server console and try again.");
      return;
    }
    showDash();
  }).catch(() => setText("gate-msg", "❌ Could not reach the server."));
});
document.getElementById("btn-brief").addEventListener("click", genBrief);
document.getElementById("btn-refresh").addEventListener("click", loadOrders);
