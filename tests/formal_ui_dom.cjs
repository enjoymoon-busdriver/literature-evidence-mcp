// Minimal DOM for deterministic delayed-response tests; rendering is checked in the browser.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const nodes = new Map();
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
    this.attributes = {}; this.listeners = {}; this.value = ''; this.checked = false;
    this.disabled = false; this.hidden = false; this.files = []; this.open = false;
    this.style = {}; this._text = ''; this.parentElement = null;
    const classes = new Set();
    this.classList = {add: (...xs) => xs.forEach(x => classes.add(x)),
      remove: (...xs) => xs.forEach(x => classes.delete(x)),
      contains: x => classes.has(x), toggle: (x, force) => {
        const on = force === undefined ? !classes.has(x) : force;
        if (on) classes.add(x); else classes.delete(x); return on;
      }};
  }
  set innerHTML(value) {
    this._html = String(value); this.children = []; this._text = this._html.replace(/<[^>]*>/g, '');
    for (const match of this._html.matchAll(/<([a-z]+)([^>]*\bid="([^"]+)"[^>]*)>/g)) {
      const node = new Element(match[1]); node.id = match[3];
      node.value = /\bvalue="([^"]*)"/.exec(match[2])?.[1] || '';
      node.disabled = /\bdisabled\b/.test(match[2]); node.checked = /\bchecked\b/.test(match[2]);
      node.hidden = /\bhidden\b/.test(match[2]); this.append(node);
    }
  }
  get innerHTML() { return this._html || ''; }
  set id(value) { this._id = value; if (value) nodes.set(value, this); }
  get id() { return this._id || ''; }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent ?? x).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'id') this.id = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  append(...children) { for (let child of children) { if (typeof child === 'string') child = {textContent: child}; child.parentElement = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(x => x !== this); }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  contains(node) { return node === this || this.children.some(x => x.contains?.(node)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const matches = node => {
      if (selector.startsWith('#')) return node.id === selector.slice(1);
      const attr = /^\[data-([a-z-]+)(?:="([^"]*)")?\]$/.exec(selector);
      if (attr) {
        const key = attr[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        return Object.hasOwn(node.dataset || {}, key) && (attr[2] === undefined || node.dataset[key] === attr[2]);
      }
      return node.tagName === selector.toUpperCase();
    };
    const result = []; const walk = node => { for (const child of node.children || []) { if (matches(child)) result.push(child); walk(child); } }; walk(this); return result;
  }
  closest(selector) { return selector === '[data-action]' && this.dataset.action ? this : this.parentElement?.closest?.(selector) || null; }
}
const html = fs.readFileSync(process.argv[2] + '/index.html', 'utf8');
const body = new Element('body');
for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const node = new Element(match[1]); node.id = match[2]; body.append(node);
}
const events = new Map();
global.document = {body, activeElement: body, createElement: tag => new Element(tag),
  createTextNode: text => ({textContent: text}), getElementById: id => nodes.get(id) || null,
  querySelector: selector => selector.startsWith('#') ? nodes.get(selector.slice(1)) || null : body.querySelector(selector),
  querySelectorAll: selector => body.querySelectorAll(selector), addEventListener(name, fn) {const list = events.get(name) || []; list.push(fn); events.set(name,list);}};
global.window = global; global.navigator = {clipboard: {writeText: async () => {}}};
global.requestAnimationFrame = fn => fn();
const requests = [];
const response = (body, ok = true) => ({ok, status: ok ? 200 : 400, headers: {get: () => 'application/json'}, json: async () => body});
let route = null;
global.fetch = (path, options = {}) => {
  assert(path.startsWith('/api/'), 'unexpected nonlocal request'); requests.push({path, options});
  if (route) { const value = route(path, options); if (value !== undefined) return value; }
  return new Promise(() => {});
};
const drain = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
Object.assign(global, {assert, nodes, Element, requests, response, drain, events});
Object.defineProperty(global, 'route', {get: () => route, set: value => {route = value;}});
for (const name of ['app.js', 'connections.js']) vm.runInThisContext(fs.readFileSync(process.argv[2] + '/' + name, 'utf8'), {filename: name});
vm.runInThisContext(`;(async () => { ${process.argv[3]} })().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });`, {filename: 'formal-ui-scenario.js'});
