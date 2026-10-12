/* Offline XSS regression test for static/app.js renderRichText.
 * Run: node tests/test_xss.js   (exit 0 = pass)
 * Uses a minimal DOM shim: the security property under test is that
 * untrusted input can never become an element *attribute* (e.g. onclick)
 * and that href is assigned as a DOM property, not via innerHTML.
 */
"use strict";
const assert = require("assert");
const path = require("path");

// ---------- minimal DOM shim ----------
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function makeEl(tag) {
  const el = {
    tag, children: [], attrs: {}, _text: null,
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    get firstChild() { return this.children[0] || null; },
    addEventListener() {},
    set textContent(v) { this.children = []; this._text = String(v); },
    get textContent() { return this._text || ""; },
    set className(v) { this.attrs["class"] = v; },
    set href(v) { this.attrs["href"] = v; },
    set target(v) { this.attrs["target"] = v; },
    set rel(v) { this.attrs["rel"] = v; },
    set colSpan(v) { this.attrs["colspan"] = v; },
    toHTML() {
      const a = Object.entries(this.attrs)
        .map(([k, v]) => ` ${k}="${esc(v)}"`).join("");
      const inner = (this._text !== null ? esc(this._text) : "") +
        this.children.map(c => (c.toHTML ? c.toHTML() : esc(c.text || ""))).join("");
      return `<${this.tag}${a}>${inner}</${this.tag}>`;
    },
  };
  return el;
}
const textNode = t => ({ text: String(t), toHTML() { return esc(this.text); } });

const messagesBox = makeEl("div");
global.document = {
  createElement: makeEl,
  createDocumentFragment() {
    return { children: [], appendChild(c) { this.children.push(c); return c; },
      toHTML() { return this.children.map(c => c.toHTML()).join(""); } };
  },
  createTextNode: textNode,
  getElementById(id) {
    if (id === "messages") return messagesBox;
    if (id === "input") return { value: "", addEventListener() {} };
    return makeEl("div");
  },
  querySelector() { return null; },
};
global.window = {};

const app = require(path.join(__dirname, "..", "static", "app.js"));
const { renderRichText } = app;

function renderToEl(text) {
  const d = makeEl("div");
  renderRichText(d, text);
  return d;
}
function walkAttrs(el, out) {
  for (const k of Object.keys(el.attrs || {})) out.push(k.toLowerCase());
  for (const c of el.children || []) if (c.tag) walkAttrs(c, out);
  return out;
}

// 1. The reported attack must not create an event-handler attribute.
{
  const evil = 'https://example.invalid/"onclick="alert`1`';
  const d = renderToEl("click " + evil + " now");
  const attrs = walkAttrs(d, []);
  assert(!attrs.some(a => a.startsWith("on")),
    "FAIL: event handler attribute created: " + JSON.stringify(attrs));
  const html = d.toHTML();
  assert(!/<a[^>]*\sonclick/i.test(html), "FAIL: onclick in serialized HTML");
  console.log("ok 1 - no onclick attribute from malicious URL");
}

// 2. javascript: scheme must not become a link.
{
  const d = renderToEl("x javascript:alert(1) y");
  const html = d.toHTML();
  assert(!html.includes("<a"), "FAIL: javascript: became a link: " + html);
  console.log("ok 2 - javascript: scheme stays plain text");
}

// 3. Normal https link still works and is inert as text.
{
  const d = renderToEl("see https://example.com/a?b=c for details");
  const html = d.toHTML();
  assert(html.includes('<a href="https://example.com/a?b=c"'),
    "FAIL: normal link missing: " + html);
  console.log("ok 3 - normal https link renders");
}

// 4. Bold still works; HTML in input stays inert text.
{
  const d = renderToEl("hi **there** <img src=x onerror=alert(1)>");
  const html = d.toHTML();
  assert(html.includes("<b>there</b>"), "FAIL: bold broken: " + html);
  assert(!html.includes("<img"), "FAIL: raw tag injected: " + html);
  assert(html.includes("&lt;img"), "FAIL: tag not escaped: " + html);
  console.log("ok 4 - bold works, tags escaped");
}

// 5. Quote-breakout inside a would-be href cannot escape the attribute.
{
  const d = renderToEl('https://x.test/"onmouseover="alert(1)');
  const attrs = walkAttrs(d, []);
  assert(!attrs.some(a => a.startsWith("on")),
    "FAIL: attribute breakout: " + JSON.stringify(attrs));
  console.log("ok 5 - quote breakout contained");
}

console.log("ALL XSS TESTS PASSED");
