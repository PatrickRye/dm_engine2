/**
 * Unit tests for DM Engine client functionality.
 *
 * Tests use a DMEngineClientCore stub with mocked fetch and DOM operations,
 * running in jsdom so DOM operations work normally.
 *
 * Run with: npm test  (from test/client/)
 */

const HEARTBEAT_RESPONSE = {
    protocol_version: 2,
    locked_characters: [],
    server_name: "TestDM",
    campaign: "TestCampaign",
    party: [
        {
            name: "Vex",
            hp: 45,
            max_hp: 50,
            current_map: "Town Square",
            is_online: true,
            is_active: true,
            is_typing: false,
            is_locked: false,
            locked_by_other: false,
        },
        {
            name: "Thornwood",
            hp: 12,
            max_hp: 40,
            current_map: "Town Square",
            is_online: true,
            is_active: false,
            is_typing: false,
            is_locked: false,
            locked_by_other: false,
        },
        {
            name: "Rat King",
            hp: 0,
            max_hp: 8,
            current_map: "Sewers",
            is_online: false,
            is_active: false,
            is_typing: false,
            is_locked: false,
            locked_by_other: false,
        },
    ],
    character_sheet: {
        sheet: {
            name: "Vex",
            hp: 45,
            max_hp: 50,
            ac: 16,
            speed: "30 ft",
            conditions: ["Prone"],
            abilities: { str: 12, dex: 18, con: 14, int: 10, wis: 13, cha: 8 },
        },
    },
    map_state: {
        map_data: { width: 1200, height: 800, walls: [], dm_map_image_path: "", pixels_per_foot: 15 },
        entities: [],
        known_traps: [],
        active_paths: [],
    },
    state_changes: [],
};

const ROLL_RESPONSE_CRIT = {
    formula: "1d20+5",
    reason: "Attack Roll",
    character: "Vex",
    rolls: [20],
    modifier: 5,
    modifier_op: "+",
    subtotal: 20,
    total: 25,
    is_crit: true,
    is_fumble: false,
    roll_str: "20",
};

const ROLL_RESPONSE_FUMBLE = {
    formula: "1d20",
    reason: "Saving Throw",
    character: "Thornwood",
    rolls: [1],
    modifier: 0,
    modifier_op: null,
    subtotal: 1,
    total: 1,
    is_crit: false,
    is_fumble: true,
    roll_str: "1",
};

const ROLL_RESPONSE_NORMAL = {
    formula: "2d6+3",
    reason: "Fireball Damage",
    character: "Vex",
    rolls: [4, 2],
    modifier: 3,
    modifier_op: "+",
    subtotal: 6,
    total: 9,
    is_crit: false,
    is_fumble: false,
    roll_str: "4, 2",
};

// ---------------------------------------------------------------------------
// Mock EventEmitter — same interface as the real one
// ---------------------------------------------------------------------------
class MockEventEmitter {
    constructor() { this._listeners = {}; }
    subscribe(event, callback) {
        if (!this._listeners[event]) this._listeners[event] = [];
        this._listeners[event].push(callback);
        return () => this.unsubscribe(event, callback);
    }
    unsubscribe(event, callback) {
        if (!this._listeners[event]) return;
        this._listeners[event] = this._listeners[event].filter(cb => cb !== callback);
    }
    publish(event, data) {
        const cbs = this._listeners[event] || [];
        for (const cb of cbs) { try { cb(data); } catch (e) {} }
    }
}

// ---------------------------------------------------------------------------
// Mock DMEngineClientCore — mirrors the real class's tested methods exactly
// ---------------------------------------------------------------------------
class MockDMEngineClientCore extends MockEventEmitter {
    constructor(view, platform) {
        super();
        this.view = view;
        this.platform = platform;
        this.activeCharacter = "Human DM";
        this.clientId = "test-uuid-0000";
        this.vaultPath = "";
        this.serverUrl = "http://127.0.0.1:8000";
        this.pollInterval = null;
        this.availableCharacters = new Set(["Human DM"]);
        this.lastUpdateCheck = 0;
        this.loadedImages = {};
        this.isMapDragging = false;
        this.isDrawingPath = false;
        this.waypoints = [];
        this.snapToGrid = true;
        this.aoeMode = null;
        this.aoeSize = 20;
        this.mouseX = 0;
        this.mouseY = 0;
        this.currentMapData = null;
        this.dmPerspectiveFilter = "ALL_PLAYERS";
        this.rollAutomations = {
            hidden_rolls: true,
            saving_throws: true,
            skill_checks: true,
            attack_rolls: true,
        };
        this.activeTypers = new Set();
        this.typingTimeout = null;
        this.lastTypedTime = 0;
        this.lastPartyData = [];
    }

    async syncState() {
        try {
            const response = await fetch(`${this.serverUrl}/heartbeat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    client_id: this.clientId,
                    character: this.activeCharacter,
                    roll_automations: this.rollAutomations,
                    include_full_state: true,
                    protocol_version: 2,
                }),
            });
            if (response.ok) {
                const data = await response.json();
                this.updateRadioUI(data.locked_characters || []);
                this.setConnectionStatus(true);
                if (data.party && data.party.length > 0) {
                    this.renderPartySidebar(data.party);
                }
                if (data.character_sheet && !data.character_sheet.error) {
                    this.renderCharacterSheet(data.character_sheet);
                }
                if (data.map_state && data.map_state.map_data) {
                    this.currentMapData = data.map_state.map_data;
                }
                const now = Date.now();
                if (now - this.lastUpdateCheck > 60000 && this.activeCharacter === "Human DM") {
                    this.lastUpdateCheck = now;
                }
            } else {
                this.setConnectionStatus(false);
            }
        } catch (e) {
            this.setConnectionStatus(false);
        }
    }

    updateRadioUI() {}
    setConnectionStatus(isLive) {
        if (!this.view.ui.statusIndicator) return;
        if (isLive) {
            this.view.ui.statusIndicator.textContent = "🟢 Live";
        } else {
            this.view.ui.statusIndicator.textContent = "🔴 Disconnected";
        }
    }

    renderPartySidebar(partyMembers) {
        if (!this.view.ui.partyList) return;
        const container = this.view.ui.partyList;
        container.innerHTML = "";
        const typingFromSSE = this.activeTypers || new Set();
        const groups = {};
        partyMembers.forEach(m => {
            const map = m.current_map || "Unknown Location";
            if (!groups[map]) groups[map] = [];
            groups[map].push(m);
        });
        for (const [map, members] of Object.entries(groups)) {
            const groupDiv = document.createElement("div");
            groupDiv.style.marginBottom = "15px";
            const mapHeader = document.createElement("h4");
            mapHeader.textContent = map;
            mapHeader.style.margin = "0 0 5px 0";
            mapHeader.style.fontSize = "0.9em";
            groupDiv.appendChild(mapHeader);
            members.forEach(m => {
                const memberDiv = document.createElement("div");
                memberDiv.style.display = "flex";
                memberDiv.style.flexDirection = "column";
                memberDiv.style.padding = "5px";
                memberDiv.style.marginBottom = "5px";
                memberDiv.style.background = "var(--background-modifier-form-field)";
                memberDiv.style.border = "1px solid var(--background-modifier-border)";
                memberDiv.style.borderRadius = "4px";
                if (m.locked_by_other) memberDiv.style.opacity = "0.6";
                const topRow = document.createElement("div");
                topRow.style.display = "flex";
                topRow.style.justifyContent = "space-between";
                topRow.style.alignItems = "center";
                const nameSpan = document.createElement("span");
                nameSpan.style.fontWeight = "bold";
                nameSpan.textContent = m.name;
                if (m.locked_by_other) nameSpan.style.textDecoration = "line-through";
                const statusSpan = document.createElement("span");
                statusSpan.style.fontSize = "1.2em";
                let statusHtml = m.is_online ? "<span>🟢</span>" : "<span style='opacity:0.5'>🔴</span>";
                if (m.is_active) statusHtml += " <span>⚡</span>";
                const isTyping = typingFromSSE.has(m.name) || m.is_typing;
                if (isTyping) statusHtml += " <span>💬</span>";
                statusSpan.innerHTML = statusHtml;
                topRow.appendChild(nameSpan);
                topRow.appendChild(statusSpan);
                const hpRow = document.createElement("div");
                hpRow.style.display = "flex";
                hpRow.style.alignItems = "center";
                hpRow.style.gap = "6px";
                hpRow.style.fontSize = "0.8em";
                const hpPct = m.max_hp > 0 ? Math.max(0, Math.min(1, m.hp / m.max_hp)) : 0;
                const hpBarWrap = document.createElement("div");
                hpBarWrap.style.flex = "1";
                hpBarWrap.style.height = "6px";
                hpBarWrap.style.borderRadius = "3px";
                hpBarWrap.style.background = "var(--background-modifier-border)";
                hpBarWrap.style.overflow = "hidden";
                const hpBarFill = document.createElement("div");
                hpBarFill.style.height = "100%";
                hpBarFill.style.borderRadius = "3px";
                hpBarFill.style.width = `${(hpPct * 100).toFixed(1)}%`;
                hpBarFill.style.transition = "width 0.3s, background 0.3s";
                if (m.hp <= 0) {
                    hpBarFill.style.background = "#dc3545";
                } else if (hpPct <= 0.25) {
                    hpBarFill.style.background = "#dc3545";
                } else if (hpPct <= 0.5) {
                    hpBarFill.style.background = "#e0a800";
                } else {
                    hpBarFill.style.background = "#28a745";
                }
                hpBarWrap.appendChild(hpBarFill);
                const hpText = document.createElement("span");
                hpText.style.color = m.hp <= 0 ? "var(--text-error)" : m.hp <= m.max_hp / 2 ? "var(--text-warning)" : "var(--text-success)";
                hpText.textContent = `${m.hp}/${m.max_hp}`;
                hpText.style.whiteSpace = "nowrap";
                hpRow.appendChild(hpBarWrap);
                hpRow.appendChild(hpText);
                memberDiv.appendChild(topRow);
                memberDiv.appendChild(hpRow);
                groupDiv.appendChild(memberDiv);
            });
            container.appendChild(groupDiv);
        }
    }

    renderCharacterSheet(data) {
        if (!data || data.error) {
            if (this.view.viewSheet) this.view.viewSheet.innerHTML = `<div>Failed to load sheet.</div>`;
            return;
        }
        const s = data.sheet;
        const hp = s.hp !== undefined ? s.hp : "?";
        const maxHp = s.max_hp !== undefined ? s.max_hp : "?";
        let condsHtml = "None";
        if (s.conditions && s.conditions.length > 0) {
            condsHtml = s.conditions.map(c => {
                const name = typeof c === "string" ? c : c.name;
                return `<span>${name}</span>`;
            }).join("");
        }
        let abilitiesHtml = "";
        if (s.abilities && Object.keys(s.abilities).length > 0) {
            const abbr = { str: "STR", dex: "DEX", con: "CON", int: "INT", wis: "WIS", cha: "CHA" };
            abilitiesHtml = `<div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:10px;">` +
                Object.entries(s.abilities).map(([k, v]) => {
                    const mod = Math.floor((v - 10) / 2);
                    const sign = mod >= 0 ? "+" : "";
                    return `<div style="background:var(--background-modifier-form-field); padding:8px; border-radius:4px; text-align:center;">
                        <div style="font-size:0.8em; color:var(--text-muted);">${abbr[k] || k.toUpperCase()}</div>
                        <div style="font-size:1.3em; font-weight:bold;">${v}</div>
                        <div style="font-size:0.85em; color:var(--text-accent);">${sign}${mod}</div>
                    </div>`;
                }).join("") + `</div>`;
        }
        const speed = s.speed || "30 ft";
        const hpPct = maxHp > 0 ? Math.max(0, Math.min(1, hp / maxHp)) : 0;
        const hpColorClass = hp <= 0 ? "var(--text-error)" : hp <= maxHp / 2 ? "var(--text-warning)" : "var(--text-success)";
        const hpBarColor = hp <= 0 ? "#dc3545" : hpPct <= 0.25 ? "linear-gradient(90deg, #dc3545, #ff4d5a)" : hpPct <= 0.5 ? "linear-gradient(90deg, #e0a800, #f0c000)" : "linear-gradient(90deg, #28a745, #34ce57)";
        if (this.view.viewSheet) this.view.viewSheet.innerHTML = `
            <h2 style="margin-top:0;">${s.name || "Unknown"}</h2>
            <div style="display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap;">
                <div style="background:var(--background-modifier-form-field); padding:10px; border-radius:5px; flex:2; min-width:160px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <b>HP</b>
                        <span style="font-size:1.1em; font-weight:bold; color:${hpColorClass}">${hp} / ${maxHp}</span>
                    </div>
                    <div style="height:8px; border-radius:4px; background:var(--background-modifier-border); overflow:hidden;">
                        <div style="height:100%; border-radius:4px; width:${(hpPct*100).toFixed(1)}%; background:${hpBarColor}; transition:width 0.3s;"></div>
                    </div>
                </div>
                <div style="background:var(--background-modifier-form-field); padding:10px; border-radius:5px; flex:1; min-width:100px; text-align:center;"><b>AC</b><br><span style="font-size:1.5em;">${s.ac || 10}</span></div>
                <div style="background:var(--background-modifier-form-field); padding:10px; border-radius:5px; flex:1; min-width:100px; text-align:center;"><b>Speed</b><br><span style="font-size:1.1em;">${speed}</span></div>
            </div>
            ${abilitiesHtml}
            <div style="margin-top:15px;">
                <b>Conditions:</b>
                <div style="margin-top:5px;">${condsHtml}</div>
            </div>`;
    }

    async rollDice(formula, reason) {
        if (!formula) return;
        try {
            const res = await fetch(`${this.serverUrl}/roll`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    formula,
                    reason: reason || "Roll",
                    character: this.activeCharacter,
                    client_id: this.clientId,
                    vault_path: this.vaultPath,
                }),
            });
            if (!res.ok) return;
            const data = await res.json();
            const isCrit = data.is_crit;
            const isFumble = data.is_fumble;
            const rollColor = isCrit ? "#ffd700" : isFumble ? "#dc3545" : "var(--text-accent)";
            const rollBorder = isCrit ? "1px solid #ffd700" : isFumble ? "1px solid #dc3545" : "1px solid var(--background-modifier-border)";
            const rollBg = isCrit ? "rgba(255,215,0,0.08)" : isFumble ? "rgba(220,53,69,0.08)" : "var(--background-modifier-form-field)";
            const rollLabel = isCrit ? "🎯 CRIT!" : isFumble ? "💀 FUMBLE" : "🎲";
            const modStr = data.modifier_op ? ` ${data.modifier_op}${data.modifier === 0 ? "" : data.modifier}` : "";
            const totalStr = `${data.total}`;
            const rollsStr = data.rolls.length > 1 ? `[${data.roll_str}]` : `[${data.roll_str}]`;
            const rollHtml = `
                <div style="background:${rollBg}; border:${rollBorder}; border-radius:6px; padding:8px 10px; margin:4px 0; max-width:340px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:bold; font-size:0.9em;">${rollLabel} ${this.activeCharacter}</span>
                        <span style="font-size:0.75em; color:var(--text-muted);">${data.formula} — ${data.reason}</span>
                    </div>
                    <div style="margin-top:4px; display:flex; align-items:center; gap:8px;">
                        <span style="font-size:1.4em; font-weight:bold; color:${rollColor};">${totalStr}</span>
                        <span style="font-size:0.85em; color:var(--text-muted);">${rollsStr}${modStr}</span>
                    </div>
                </div>`;
            const msgDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-message" });
            msgDiv.style.marginBottom = "4px";
            msgDiv.style.lineHeight = "1.4";
            msgDiv.innerHTML = rollHtml;
            this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;
            this._gcChatHistory();
        } catch (e) {
            new Notice("Dice roll failed.");
        }
    }

    _gcChatHistory() {
        const MAX_MESSAGES = 200;
        const chatHistory = this.view.ui.chatHistory;
        if (!chatHistory) return;
        const messages = Array.from(chatHistory.querySelectorAll(".dm-message"));
        while (messages.length > MAX_MESSAGES) {
            messages[0].remove();
            messages.shift();
        }
    }
}

// ---------------------------------------------------------------------------
// DI container
// ---------------------------------------------------------------------------
function createCore(overrides = {}) {
    const partyList    = document.createElement("div");
    const chatHistory  = document.createElement("div");
    const chatInput    = document.createElement("textarea");
    const statusIndicator = document.createElement("span");
    const viewSheet    = document.createElement("div");
    const viewMaps     = document.createElement("div");
    // viewSheet/viewMaps are direct properties (renderCharacterSheet/renderMaps access this.view.viewSheet)
    // chat-related UI is under ui (renderPartySidebar accesses this.view.ui.partyList)
    const ui = { partyList, chatHistory, chatInput, statusIndicator };
    const view = { ui, viewSheet, viewMaps };
    const core = new MockDMEngineClientCore(view, "web");
    core.pollInterval = null;
    core.activeCharacter = "Vex";
    Object.assign(core, overrides);
    return core;
}

// ---------------------------------------------------------------------------
// Shared fetch spy
// ---------------------------------------------------------------------------
let fetchSpy;
beforeEach(() => {
    fetchSpy = jest.spyOn(global, "fetch");
});
afterEach(() => {
    fetchSpy.mockRestore();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("rollDice", () => {

    test("renders a crit roll with gold styling", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(ROLL_RESPONSE_CRIT),
        });

        await core.rollDice("1d20+5", "Attack Roll");

        const msgDiv = core.view.ui.chatHistory.querySelector(".dm-message");
        expect(msgDiv).not.toBeNull();
        expect(msgDiv.innerHTML).toContain("🎯 CRIT!");
        expect(msgDiv.innerHTML).toContain("25");
        expect(msgDiv.innerHTML).toContain("20");
        expect(msgDiv.innerHTML).toContain("#ffd700");
    });

    test("renders a fumble roll with red styling", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(ROLL_RESPONSE_FUMBLE),
        });

        await core.rollDice("1d20", "Saving Throw");

        const msgDiv = core.view.ui.chatHistory.querySelector(".dm-message");
        expect(msgDiv.innerHTML).toContain("💀 FUMBLE");
        expect(msgDiv.innerHTML).toContain("1");
        expect(msgDiv.innerHTML).toContain("#dc3545");
    });

    test("renders a normal roll without special labels", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(ROLL_RESPONSE_NORMAL),
        });

        await core.rollDice("2d6+3", "Fireball Damage");

        const msgDiv = core.view.ui.chatHistory.querySelector(".dm-message");
        expect(msgDiv.innerHTML).toContain("🎲");
        expect(msgDiv.innerHTML).not.toContain("CRIT");
        expect(msgDiv.innerHTML).not.toContain("FUMBLE");
        expect(msgDiv.innerHTML).toContain("9");
    });

    test("calls fetch with correct /roll endpoint and body", async () => {
        const core = createCore({ activeCharacter: "Thornwood" });
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(ROLL_RESPONSE_NORMAL),
        });

        await core.rollDice("2d6+3", "Sneak Attack");

        expect(fetchSpy).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchSpy.mock.calls[0];
        expect(url).toMatch(/\/roll$/);
        const body = JSON.parse(opts.body);
        expect(body.formula).toBe("2d6+3");
        expect(body.reason).toBe("Sneak Attack");
        expect(body.character).toBe("Thornwood");
        expect(body.client_id).toBeDefined();
    });

    test("early returns when formula is empty", async () => {
        const core = createCore();
        await core.rollDice("", "should not call fetch");
        expect(fetchSpy).not.toHaveBeenCalled();
    });
});

describe("renderPartySidebar", () => {

    test("renders HP bar for each party member", () => {
        const core = createCore();
        core.renderPartySidebar(HEARTBEAT_RESPONSE.party);
        // HP bars are the divs with height:6px inside each member entry
        const hpBars = core.view.ui.partyList.querySelectorAll("div[style*='height: 6px']");
        expect(hpBars.length).toBe(3); // 3 party members
    });

    test("Vex (45/50 HP, 90%) gets green HP bar", () => {
        const core = createCore();
        core.renderPartySidebar(HEARTBEAT_RESPONSE.party);
        // Find member divs: they are siblings of h4 headers inside groupDivs
        // Each groupDiv contains an h4 + member divs. Use the h4 to locate the group,
        // then find the member div by name and HP bar.
        const groupDivs = core.view.ui.partyList.querySelectorAll("div");
        let vexDiv = null;
        for (const g of groupDivs) {
            const members = g.querySelectorAll("div");
            for (const m of members) {
                if (m.textContent.includes("Vex") && m.querySelector("div[style*='height: 6px']")) {
                    vexDiv = m;
                    break;
                }
            }
            if (vexDiv) break;
        }
        expect(vexDiv).toBeDefined();
        const hpBarWrap = vexDiv.querySelector("div[style*='height: 6px']");
        const hpBarFill = hpBarWrap.querySelector("div");
        // jsdom normalizes #28a745 to rgb(40, 167, 69)
        expect(hpBarFill.style.background).toMatch(/#28a745|rgb\(40,\s*167,\s*69\)/);
    });

    test("Thornwood (12/40 HP, 30%) gets yellow HP bar", () => {
        const core = createCore();
        core.renderPartySidebar(HEARTBEAT_RESPONSE.party);
        const groupDivs = core.view.ui.partyList.querySelectorAll("div");
        let thornwoodDiv = null;
        for (const g of groupDivs) {
            const members = g.querySelectorAll("div");
            for (const m of members) {
                if (m.textContent.includes("Thornwood") && m.querySelector("div[style*='height: 6px']")) {
                    thornwoodDiv = m;
                    break;
                }
            }
            if (thornwoodDiv) break;
        }
        expect(thornwoodDiv).toBeDefined();
        const hpBarWrap = thornwoodDiv.querySelector("div[style*='height: 6px']");
        const hpBarFill = hpBarWrap.querySelector("div");
        // 30% HP → yellow (≤50% but >25%); jsdom normalizes #e0a800 to rgb(224, 168, 0)
        expect(hpBarFill.style.background).toMatch(/#e0a800|rgb\(224,\s*168,\s*0\)/);
    });

    test("Rat King (0 HP, dead) gets red HP bar", () => {
        const core = createCore();
        core.renderPartySidebar(HEARTBEAT_RESPONSE.party);
        const groupDivs = core.view.ui.partyList.querySelectorAll("div");
        let ratKingDiv = null;
        for (const g of groupDivs) {
            const members = g.querySelectorAll("div");
            for (const m of members) {
                if (m.textContent.includes("Rat King") && m.querySelector("div[style*='height: 6px']")) {
                    ratKingDiv = m;
                    break;
                }
            }
            if (ratKingDiv) break;
        }
        expect(ratKingDiv).toBeDefined();
        const hpBarWrap = ratKingDiv.querySelector("div[style*='height: 6px']");
        const hpBarFill = hpBarWrap.querySelector("div");
        // 0 HP → red; jsdom normalizes #dc3545 to rgb(220, 53, 69)
        expect(hpBarFill.style.background).toMatch(/#dc3545|rgb\(220,\s*53,\s*69\)/);
    });

    test("groups members by current_map location", () => {
        const core = createCore();
        core.renderPartySidebar(HEARTBEAT_RESPONSE.party);
        const headers = core.view.ui.partyList.querySelectorAll("h4");
        const headerTexts = Array.from(headers).map(h => h.textContent);
        expect(headerTexts).toContain("Town Square");
        expect(headerTexts).toContain("Sewers");
    });

    test("shows typing indicator when is_typing is true", () => {
        const core = createCore();
        const partyWithTyping = HEARTBEAT_RESPONSE.party.map(m =>
            m.name === "Vex" ? { ...m, is_typing: true } : m
        );
        core.renderPartySidebar(partyWithTyping);
        const vexDiv = Array.from(core.view.ui.partyList.querySelectorAll("div"))
            .find(d => d.textContent.includes("Vex"));
        expect(vexDiv.innerHTML).toContain("💬");
    });
});

describe("renderCharacterSheet", () => {

    test("renders HP bar in the character sheet", () => {
        const core = createCore();
        const sheet = HEARTBEAT_RESPONSE.character_sheet;
        core.renderCharacterSheet(sheet);
        // HP bar: height:8px inside the HP stat box (viewSheet is direct property)
        const hpBars = core.view.viewSheet.querySelectorAll("div");
        const hasHpBar = Array.from(hpBars).some(d => d.style.height === "8px");
        expect(hasHpBar).toBe(true);
    });

    test("renders 45/50 HP text", () => {
        const core = createCore();
        core.renderCharacterSheet(HEARTBEAT_RESPONSE.character_sheet);
        expect(core.view.viewSheet.innerHTML).toContain("45");
        expect(core.view.viewSheet.innerHTML).toContain("50");
    });

    test("renders Prone condition", () => {
        const core = createCore();
        core.renderCharacterSheet(HEARTBEAT_RESPONSE.character_sheet);
        expect(core.view.viewSheet.innerHTML).toContain("Prone");
    });

    test("renders AC and Speed", () => {
        const core = createCore();
        core.renderCharacterSheet(HEARTBEAT_RESPONSE.character_sheet);
        expect(core.view.viewSheet.innerHTML).toContain("AC");
        expect(core.view.viewSheet.innerHTML).toContain("16");
        expect(core.view.viewSheet.innerHTML).toContain("30 ft");
    });

    test("renders ability scores in a 3-column grid", () => {
        const core = createCore();
        core.renderCharacterSheet(HEARTBEAT_RESPONSE.character_sheet);
        // 6 ability boxes (str, dex, con, int, wis, cha) in a 3-column grid
        const gridCols = core.view.viewSheet.innerHTML;
        expect(gridCols).toContain("STR");
        expect(gridCols).toContain("DEX");
        expect(gridCols).toContain("18"); // DEX mod = +4
    });

    test("handles error response gracefully", () => {
        const core = createCore();
        core.renderCharacterSheet({ error: "Character not found" });
        // Real code outputs a generic "Failed to load" div for errors
        expect(core.view.viewSheet.innerHTML).toContain("Failed to load");
    });

    test("handles null data gracefully", () => {
        const core = createCore();
        core.renderCharacterSheet(null);
        expect(core.view.viewSheet.innerHTML).toContain("Failed to load");
    });
});

describe("_gcChatHistory", () => {

    test("removes oldest messages when count exceeds 200", () => {
        const core = createCore();
        const chatHistory = core.view.ui.chatHistory;
        for (let i = 0; i < 205; i++) {
            const div = document.createElement("div");
            div.className = "dm-message";
            div.textContent = `msg-${i}`;
            chatHistory.appendChild(div);
        }
        core._gcChatHistory();
        const remaining = chatHistory.querySelectorAll(".dm-message");
        expect(remaining.length).toBe(200);
        expect(chatHistory.textContent).not.toContain("msg-0");
        expect(chatHistory.textContent).toContain("msg-204");
    });

    test("does nothing when messages are below the limit", () => {
        const core = createCore();
        const chatHistory = core.view.ui.chatHistory;
        for (let i = 0; i < 50; i++) {
            const div = document.createElement("div");
            div.className = "dm-message";
            div.textContent = `msg-${i}`;
            chatHistory.appendChild(div);
        }
        core._gcChatHistory();
        expect(chatHistory.querySelectorAll(".dm-message").length).toBe(50);
    });

    test("handles empty chatHistory gracefully", () => {
        const core = createCore();
        expect(() => core._gcChatHistory()).not.toThrow();
    });
});

describe("syncState", () => {

    test("sends heartbeat with include_full_state: true and protocol_version: 2", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(HEARTBEAT_RESPONSE),
        });

        await core.syncState();

        expect(fetchSpy).toHaveBeenCalledTimes(1);
        const [url, opts] = fetchSpy.mock.calls[0];
        expect(url).toMatch(/\/heartbeat$/);
        const body = JSON.parse(opts.body);
        expect(body.include_full_state).toBe(true);
        expect(body.protocol_version).toBe(2);
    });

    test("renders party sidebar from inlined heartbeat data", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(HEARTBEAT_RESPONSE),
        });

        await core.syncState();

        expect(core.view.ui.partyList.textContent).toContain("Vex");
        expect(core.view.ui.partyList.textContent).toContain("Thornwood");
        expect(core.view.ui.partyList.textContent).toContain("Rat King");
    });

    test("renders character sheet from inlined heartbeat data", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(HEARTBEAT_RESPONSE),
        });

        await core.syncState();

        expect(core.view.viewSheet.textContent).toContain("Vex");
        expect(core.view.viewSheet.textContent).toContain("45");
        expect(core.view.viewSheet.textContent).toContain("50");
    });

    test("updates connection status to live on success", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve(HEARTBEAT_RESPONSE),
        });

        await core.syncState();

        expect(core.view.ui.statusIndicator.textContent).toContain("🟢");
    });

    test("updates connection status to disconnected on network failure", async () => {
        const core = createCore();
        fetchSpy.mockRejectedValueOnce(new Error("network error"));

        await core.syncState();

        expect(core.view.ui.statusIndicator.textContent).toContain("🔴");
    });
});

describe("dice bar (d20 + modifier)", () => {

    test("d20 roll with no modifier renders crit", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve({
                formula: "1d20", reason: "d20 roll", character: "Vex",
                rolls: [20], modifier: 0, modifier_op: null,
                subtotal: 20, total: 20, is_crit: true, is_fumble: false, roll_str: "20",
            }),
        });

        await core.rollDice("1d20", "d20 roll");

        const msgDiv = core.view.ui.chatHistory.querySelector(".dm-message");
        expect(msgDiv.innerHTML).toContain("🎯 CRIT!");
        expect(msgDiv.innerHTML).toContain("20");
    });

    test("d20+5 roll renders total 19", async () => {
        const core = createCore();
        fetchSpy.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve({
                formula: "1d20+5", reason: "Attack Roll", character: "Vex",
                rolls: [14], modifier: 5, modifier_op: "+",
                subtotal: 14, total: 19, is_crit: false, is_fumble: false, roll_str: "14",
            }),
        });

        await core.rollDice("1d20+5", "Attack Roll");

        const msgDiv = core.view.ui.chatHistory.querySelector(".dm-message");
        expect(msgDiv.innerHTML).toContain("🎲");
        expect(msgDiv.innerHTML).toContain("19");
        expect(msgDiv.innerHTML).toContain("14");
    });
});
