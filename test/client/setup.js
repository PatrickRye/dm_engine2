/**
 * Jest setup — runs before each test file.
 * Mocks global browser APIs that the client code depends on in Node.js.
 */

// ---------------------------------------------------------------------------
// DOM polyfills (platform === "web" codepath)
// ---------------------------------------------------------------------------
if (typeof HTMLElement !== "undefined" && !HTMLElement.prototype.createEl) {
    HTMLElement.prototype.empty = function () { this.innerHTML = ""; };
    HTMLElement.prototype.createEl = function (tag, opt) {
        const el = document.createElement(tag);
        if (opt) {
            if (opt.cls)  el.className  = opt.cls;
            if (opt.text) el.textContent = opt.text;
            if (opt.type)  el.type       = opt.type;
            if (opt.value) el.value     = opt.value;
            if (opt.name)  el.name      = opt.name;
            if (opt.margin) el.style.margin = opt.margin;
        }
        this.appendChild(el);
        return el;
    };
    HTMLElement.prototype.createDiv  = function (opt) { return this.createEl("div", opt); };
    HTMLElement.prototype.createSpan = function (opt) { return this.createEl("span", opt); };
}

// ---------------------------------------------------------------------------
// Notice mock
// ---------------------------------------------------------------------------
global.Notice = class Notice {
    constructor(msg) { /* swallow */ }
};

// ---------------------------------------------------------------------------
// crypto.randomUUID
// ---------------------------------------------------------------------------
if (!global.crypto) global.crypto = {};
if (!global.crypto.randomUUID) {
    global.crypto.randomUUID = () => "test-uuid-0000-0000-000000000000";
}

// ---------------------------------------------------------------------------
// fetch — jsdom doesn't include fetch; tests override with
// jest.spyOn(global, "fetch").mockResolvedValueOnce()
// ---------------------------------------------------------------------------
global.fetch = jest.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
);

// ---------------------------------------------------------------------------
// localStorage
// ---------------------------------------------------------------------------
if (!global.localStorage) {
    global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
}
