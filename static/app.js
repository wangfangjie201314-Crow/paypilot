let sessionId = "demo-" + Math.random().toString(36).slice(2, 8);
let lastOrderId = null, pollTimer = null;

// ---- safe rich-text rendering -------------------------------------------
// User/assistant text is NEVER concatenated into innerHTML. Bold (**x**) and
// links are built with DOM APIs; textContent keeps everything inert, and
// href is assigned as a property (a quote inside the string cannot break
// out of an attribute). Only http/https URLs become links.
const URL_RE = /(https?:\/\/[^\s)]+)/g;

function safeUrl(s) {
  try {
    const u = new URL(s);
    return (u.protocol === "http:" || u.protocol === "https:") ? s : null;
  } catch (e) {
    return null;
  }
}

function appendBoldAware(parent, text) {
  // splits on **bold** markers; everything lands in textContent / <b>
  const parts = text.split(/(\*\*.+?\*\*)/g);
  for (const p of parts) {
    if (p.length > 4 && p.startsWith("**") && p.endsWith("**")) {
      const b = document.createElement("b");
      b.textContent = p.slice(2, -2);
      parent.appendChild(b);
    } else if (p) {
      parent.appendChild(document.createTextNode(p));
    }
  }
}

function renderRichText(el, text) {
  const frag = document.createDocumentFragment();
  let last = 0, m;
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(text)) !== null) {
    appendBoldAware(frag, text.slice(last, m.index));
    const href = safeUrl(m[1]);
    if (href) {
      const a = document.createElement("a");
      a.href = href;               // property assignment: no attribute breakout
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = m[1];
      frag.appendChild(a);
    } else {
      frag.appendChild(document.createTextNode(m[1]));
    }
    last = m.index + m[1].length;
  }
  appendBoldAware(frag, text.slice(last));
  el.appendChild(frag);
}

function addMsg(text, who) {
  const d = document.createElement("div");
  d.className = "msg " + who;
  renderRichText(d, text);
  const box = document.getElementById("messages");
  box.appendChild(d); box.scrollTop = box.scrollHeight;
}

function send(text, retryKey) {
  // Retry identity (ARCHITECTURE.md §5): one key per user message. If the
  // request fails on the network, the "Retry" button re-sends the SAME
  // message with the SAME key, so the retry cannot mint a duplicate order.
  const key = retryKey || (crypto.randomUUID ? crypto.randomUUID()
                                            : String(Date.now()));
  addMsg(text, "user");
  document.getElementById("input").value = "";
  addMsg("PayPilot is thinking…", "sys");
  fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({session_id: sessionId, message: text,
                          idempotency_key: key})})
    .then(r => r.json()).then(d => {
      document.querySelector(".msg.sys")?.remove();
      addMsg(d.reply, "bot");
      if (d.order_id) { lastOrderId = d.order_id; startPolling(); }
    })
    .catch(() => {
      document.querySelector(".msg.sys")?.remove();
      const box = document.getElementById("messages");
      const d = document.createElement("div");
      d.className = "msg sys";
      d.appendChild(document.createTextNode("⚠️ Network error — nothing was sent twice. "));
      const btn = document.createElement("button");
      btn.className = "linkbtn";
      btn.textContent = "Retry";
      btn.addEventListener("click", () => { d.remove(); send(text, key); });
      d.appendChild(btn);
      box.appendChild(d); box.scrollTop = box.scrollHeight;
    });
}
function sendInput() {
  const v = document.getElementById("input").value.trim();
  if (v) send(v);
}
document.getElementById("input").addEventListener("keydown", e => { if (e.key === "Enter") sendInput(); });

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    const url = "/api/order/" + encodeURIComponent(lastOrderId) +
                "/status?session_id=" + encodeURIComponent(sessionId);
    fetch(url).then(r => r.json()).then(d => {
      if (d.status === "CAPTURED") {
        clearInterval(pollTimer);
        addMsg("✅ Payment confirmed! Thank you so much — your ceramics will ship within 2 business days. I'll send tracking as soon as it's on its way!", "bot");
      }
    });
  }, 3000);
}

// greeting
addMsg("👋 Hi there! I'm **PayPilot**, the AI sales assistant for Mira Ceramics. Ask me about our handmade mugs, vases or coffee sets — I can even check you out with PayPal right here in chat!", "bot");

// Export pure rendering helpers for offline tests (harmless in browsers).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {renderRichText, safeUrl, appendBoldAware, URL_RE};
}
