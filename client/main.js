let requestUrl, ItemView, Plugin, Notice, MarkdownRenderer;
if (typeof require !== "undefined") {
    try {
        const obsidian = require('obsidian');
        requestUrl = obsidian.requestUrl;
        ItemView = obsidian.ItemView;
        Plugin = obsidian.Plugin;
        Notice = obsidian.Notice;
        MarkdownRenderer = obsidian.MarkdownRenderer;
    } catch (e) { }
}
if (!ItemView) {
    ItemView = class { };
    Plugin = class { };
    Notice = class { constructor(msg) { console.log("Notice:", msg); alert(msg); } };
    MarkdownRenderer = {
        renderMarkdown: async (text, el) => {
            if (typeof marked !== "undefined") el.innerHTML = marked.parse(text);
            else el.innerHTML = `<p>${text}</p>`;
        }
    };
}

const VIEW_TYPE_DM_CHAT = "dm-chat-view";

// ----------------------------------------------------------------
// EventEmitter — minimal pub/sub for subsystem decoupling
// ----------------------------------------------------------------
class EventEmitter {
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
        for (const cb of cbs) {
            try { cb(data); } catch (e) { console.error(`Event handler error (${event}):`, e); }
        }
    }
}

// ----------------------------------------------------------------
// MapRenderer — owns all Canvas 2D rendering state and drawScene
// ----------------------------------------------------------------
class MapRenderer {
    constructor(core, emitter) {
        this.core = core;           // DMEngineClientCore (for serverUrl, loadedImages, etc.)
        this.emitter = emitter;     // EventEmitter (unused in Phase 1, ready for Phase 2+)
        this.ctx = null;
        this.canvas = null;
        this.bgImageRef = null;
        this.mapData = null;
        this.entities = [];
        this.knownTraps = [];
        this.activePaths = [];
        this.SCALE = 15;
        this.activePings = [];
        this.pingAnimationId = null;
        this.drawSceneRef = null;
    }

    is_visible_to_player(x, y) {
        const explored = this.mapData?.explored_areas || [];
        for (const area of explored) {
            const [ax, ay, radius] = area;
            if (Math.hypot(x - ax, y - ay) <= radius) return true;
        }
        return false;
    }

    drawScene(bgImg) {
        const bgImageRef = bgImg || this.bgImageRef;
        this.bgImageRef = bgImageRef;
        const { mapData, entities, knownTraps, activePaths, ctx, SCALE, canvas } = this;
        if (!ctx || !mapData) return;
        this.drawSceneRef = this.drawScene.bind(this);

        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (bgImageRef) ctx.drawImage(bgImageRef, 0, 0);

        // Grid
        ctx.strokeStyle = "rgba(255, 255, 255, 0.05)"; ctx.lineWidth = 1;
        for (let i = 0; i < canvas.width; i += SCALE * mapData.grid_scale) {
            ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, canvas.height); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(canvas.width, i); ctx.stroke();
        }

        // Fog of War
        ctx.fillStyle = this.core.activeCharacter !== "Human DM" ? "rgba(0, 0, 0, 0.98)" : "rgba(0, 0, 50, 0.4)";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.globalCompositeOperation = 'destination-out';
        (mapData.explored_areas || []).forEach(area => {
            const [x, y, radius] = area;
            ctx.beginPath(); ctx.arc(x * SCALE, y * SCALE, radius * SCALE, 0, Math.PI * 2); ctx.fill();
        });
        ctx.globalCompositeOperation = 'source-over';

        // Walls
        const activeWalls = [...(mapData.walls || []), ...(mapData.temporary_walls || [])];
        activeWalls.forEach(wall => {
            if (!this.is_visible_to_player(wall.start[0], wall.start[1]) && !this.is_visible_to_player(wall.end[0], wall.end[1])) return;
            ctx.beginPath(); ctx.moveTo(wall.start[0] * SCALE, wall.start[1] * SCALE);
            ctx.lineTo(wall.end[0] * SCALE, wall.end[1] * SCALE);
            if (!wall.is_solid && wall.is_visible) { ctx.strokeStyle = "rgba(40, 167, 69, 0.6)"; ctx.lineWidth = 4; }
            else if (!wall.is_visible) { ctx.strokeStyle = "rgba(0, 150, 255, 0.4)"; ctx.lineWidth = 2; }
            else { ctx.strokeStyle = "rgba(220, 53, 69, 0.8)"; ctx.lineWidth = 3; }
            ctx.stroke();
        });

        // Set up entity icon loading + draw entities
        entities.forEach(ent => {
            if (!ent.icon_url) return;
            const imgKey = ent.name + "|" + ent.icon_url;
            if (this.core.loadedImages[imgKey] === undefined) {
                this.core.loadedImages[imgKey] = "loading";
                const img = new Image();
                img.onload = () => { this.core.loadedImages[imgKey] = img; this.drawScene(); };
                img.onerror = () => { this.core.loadedImages[imgKey] = "failed"; this.drawScene(); };
                img.src = `${this.core.serverUrl}/vault_media?filepath=${encodeURIComponent(ent.icon_url)}`;
            }
        });

        entities.forEach(ent => {
            if (ent.hp <= 0) return;
            const px = ent.x * SCALE, py = ent.y * SCALE, pRadius = (ent.size / 2) * SCALE;
            if (this.core.activeCharacter !== "Human DM" && !ent.is_pc) {
                if (!this.is_visible_to_player(ent.x, ent.y)) return;
            }
            ctx.beginPath(); ctx.arc(px, py, pRadius, 0, Math.PI * 2);
            const imgKey = ent.name + "|" + (ent.icon_url || "");
            const imgState = this.core.loadedImages[imgKey];
            if (ent.icon_url && imgState instanceof Image) {
                ctx.save(); ctx.clip();
                ctx.drawImage(imgState, px - pRadius, py - pRadius, pRadius * 2, pRadius * 2);
                ctx.restore();
            } else if (ent.icon_url && imgState === "loading") {
                ctx.save(); ctx.clip();
                ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545"; ctx.fill();
                ctx.strokeStyle = "rgba(255,255,255,0.7)"; ctx.lineWidth = 2;
                ctx.beginPath(); ctx.arc(px, py, pRadius * 0.6, 0, Math.PI * 1.5); ctx.stroke();
                ctx.restore();
            } else if (ent.icon_url && imgState === "failed") {
                ctx.fillStyle = "#555"; ctx.fill();
                ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(px - pRadius * 0.4, py - pRadius * 0.4); ctx.lineTo(px + pRadius * 0.4, py + pRadius * 0.4);
                ctx.moveTo(px + pRadius * 0.4, py - pRadius * 0.4); ctx.lineTo(px - pRadius * 0.4, py + pRadius * 0.4);
                ctx.stroke();
            } else {
                ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545"; ctx.fill();
            }
            ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2; ctx.stroke();
            ctx.fillStyle = "white"; ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center";
            ctx.fillText(ent.name, px, py - pRadius - 5);
        });

        // Traps
        knownTraps.forEach(trap => {
            if (this.is_visible_to_player(trap.x, trap.y)) {
                ctx.fillStyle = "red"; ctx.font = "bold 20px sans-serif"; ctx.textAlign = "center";
                ctx.fillText("X", trap.x * SCALE, trap.y * SCALE);
            }
        });

        // Paths
        activePaths.forEach(p => {
            if (this.core.activeCharacter !== "Human DM" && p.entity_name !== this.core.activeCharacter) return;
            if (p.waypoints && p.waypoints.length > 1) {
                ctx.beginPath();
                ctx.moveTo(p.waypoints[0][0] * SCALE, p.waypoints[0][1] * SCALE);
                for (let i = 1; i < p.waypoints.length; i++) ctx.lineTo(p.waypoints[i][0] * SCALE, p.waypoints[i][1] * SCALE);
                ctx.strokeStyle = p.is_valid ? "rgba(255, 165, 0, 0.8)" : "rgba(220, 53, 69, 0.8)";
                ctx.lineWidth = 3; ctx.setLineDash([5, 5]); ctx.stroke(); ctx.setLineDash([]);
            }
            if (!p.is_valid && p.alternative_path && p.alternative_path.length > 1) {
                ctx.beginPath();
                ctx.moveTo(p.alternative_path[0][0] * SCALE, p.alternative_path[0][1] * SCALE);
                for (let i = 1; i < p.alternative_path.length; i++) ctx.lineTo(p.alternative_path[i][0] * SCALE, p.alternative_path[i][1] * SCALE);
                ctx.strokeStyle = "rgba(255, 255, 0, 1.0)"; ctx.lineWidth = 4; ctx.stroke();
            }
        });

        // Drag path
        if (this.core.isMapDragging && canvas.draggedEntity && canvas.dragStartX !== undefined && canvas.dragStartY !== undefined) {
            const startX = canvas.dragStartX * SCALE, startY = canvas.dragStartY * SCALE;
            const currentX = canvas.draggedEntity.x * SCALE, currentY = canvas.draggedEntity.y * SCALE;
            ctx.beginPath(); ctx.moveTo(startX, startY); ctx.lineTo(currentX, currentY);
            ctx.strokeStyle = this.core.activeCharacter === "Human DM" ? "rgba(255, 200, 0, 0.8)" : "rgba(40, 167, 69, 0.8)";
            ctx.lineWidth = 4; ctx.setLineDash([8, 6]); ctx.stroke(); ctx.setLineDash([]);
            const distFt = Math.max(Math.abs(canvas.draggedEntity.x - canvas.dragStartX), Math.abs(canvas.draggedEntity.y - canvas.dragStartY));
            ctx.fillStyle = "white"; ctx.font = "bold 14px sans-serif"; ctx.textAlign = "center";
            ctx.fillText(`${Math.round(distFt)} ft`, (startX + currentX) / 2, (startY + currentY) / 2 - 10);
        }

        // AOE
        if (this.core.aoeMode) {
            ctx.fillStyle = "rgba(255, 100, 0, 0.3)"; ctx.strokeStyle = "rgba(255, 100, 0, 0.8)"; ctx.lineWidth = 2;
            const sizePx = this.core.aoeSize * SCALE, mx = this.core.mouseX * SCALE, my = this.core.mouseY * SCALE;
            const activeEnt = entities.find(e => e.name === this.core.activeCharacter);
            if (activeEnt) {
                const ex = activeEnt.x * SCALE, ey = activeEnt.y * SCALE;
                ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(mx, my);
                ctx.strokeStyle = "rgba(255, 255, 255, 0.6)"; ctx.lineWidth = 2; ctx.setLineDash([4, 4]); ctx.stroke(); ctx.setLineDash([]);
                const distFt = Math.max(Math.abs(this.core.mouseX - activeEnt.x), Math.abs(this.core.mouseY - activeEnt.y));
                const text = `${Math.round(distFt)} ft`, midX = (ex + mx) / 2, midY = (ey + my) / 2 - 10;
                ctx.font = "bold 14px sans-serif"; ctx.textAlign = "center"; ctx.lineWidth = 3; ctx.strokeStyle = "black";
                ctx.strokeText(text, midX, midY); ctx.fillStyle = "white"; ctx.fillText(text, midX, midY);
                ctx.fillStyle = "rgba(255, 100, 0, 0.3)"; ctx.strokeStyle = "rgba(255, 100, 0, 0.8)"; ctx.lineWidth = 2;
            }
            if (this.core.aoeMode === "circle") { ctx.beginPath(); ctx.arc(mx, my, sizePx, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
            else if (this.core.aoeMode === "cube") { ctx.fillRect(mx - sizePx / 2, my - sizePx / 2, sizePx, sizePx); ctx.strokeRect(mx - sizePx / 2, my - sizePx / 2, sizePx, sizePx); }
            else if (this.core.aoeMode === "cone" || this.core.aoeMode === "line") {
                if (activeEnt) {
                    const ex = activeEnt.x * SCALE, ey = activeEnt.y * SCALE;
                    const angle = Math.atan2(my - ey, mx - ex);
                    ctx.beginPath(); ctx.moveTo(ex, ey);
                    if (this.core.aoeMode === "cone") ctx.arc(ex, ey, sizePx, angle - Math.PI / 6, angle + Math.PI / 6);
                    else {
                        const halfWidth = (5 / 2) * SCALE;
                        const p1x = ex - halfWidth * Math.sin(angle), p1y = ey + halfWidth * Math.cos(angle);
                        const p2x = ex + halfWidth * Math.sin(angle), p2y = ey - halfWidth * Math.cos(angle);
                        const p3x = p2x + sizePx * Math.cos(angle), p3y = p2y + sizePx * Math.sin(angle);
                        const p4x = p1x + sizePx * Math.cos(angle), p4y = p1y + sizePx * Math.sin(angle);
                        ctx.lineTo(p1x, p1y); ctx.lineTo(p2x, p2y); ctx.lineTo(p3x, p3y); ctx.lineTo(p4x, p4y);
                    }
                    ctx.closePath(); ctx.fill(); ctx.stroke();
                } else {
                    ctx.fillStyle = "white"; ctx.font = "bold 14px sans-serif";
                    ctx.fillText("Select your character to project lines/cones", mx, my - 10);
                }
            }
        }

        // Pings
        if (this.activePings) {
            const now = Date.now();
            this.activePings = this.activePings.filter(p => now - p.time < 3000);
            this.activePings.forEach(p => {
                const age = now - p.time;
                if (age > 3000) return;
                const maxRadius = 45, progress = age / 1000, pulse = progress % 1;
                const radius = pulse * maxRadius, alpha = 1 - pulse;
                const px = p.x * SCALE, py = p.y * SCALE;
                ctx.beginPath(); ctx.arc(px, py, Math.max(0.1, radius), 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(220, 53, 69, ${alpha})`; ctx.lineWidth = 3; ctx.stroke();
                ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(220, 53, 69, ${Math.max(0, 1 - age / 3000)})`; ctx.fill();
                ctx.fillStyle = `rgba(255, 255, 255, ${Math.max(0, 1 - age / 3000)})`;
                ctx.font = "bold 14px sans-serif"; ctx.textAlign = "center";
                ctx.fillText(p.character, px, py - 20);
            });
            if (this.activePings.length > 0 && !this.pingAnimationId) {
                this.pingAnimationId = requestAnimationFrame(() => this._animatePings());
            } else if (this.activePings.length === 0) {
                this.pingAnimationId = null;
            }
        }
    }

    _animatePings() {
        if (!this.activePings || this.activePings.length === 0) { this.pingAnimationId = null; return; }
        this.drawScene();
        this.pingAnimationId = requestAnimationFrame(() => this._animatePings());
    }

    animatePings() {
        this.activePings = this.activePings || [];
        const now = Date.now();
        this.activePings = this.activePings.filter(p => now - p.time < 3000);
        if (this.drawSceneRef) this.drawSceneRef();
        if (this.activePings.length > 0) {
            this.pingAnimationId = requestAnimationFrame(() => this._animatePings());
        } else {
            this.pingAnimationId = null;
        }
    }

    // Called by renderMaps to hand off canvas + map state for the current render cycle
    beginRender({ mapData, entities, knownTraps, activePaths, canvas, imagePath }) {
        this.mapData = mapData;
        this.entities = entities;
        this.knownTraps = knownTraps;
        this.activePaths = activePaths;
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');

        if (imagePath) {
            if (this.core.loadedImages[imagePath] instanceof Image) {
                canvas.width = this.core.loadedImages[imagePath].width;
                canvas.height = this.core.loadedImages[imagePath].height;
                this.drawScene(this.core.loadedImages[imagePath]);
            } else if (this.core.loadedImages[imagePath] === "failed") {
                this.drawScene(null);
            } else if (this.core.loadedImages[imagePath] === undefined) {
                this.core.loadedImages[imagePath] = "loading";
                const img = new Image();
                img.onload = () => {
                    this.core.loadedImages[imagePath] = img;
                    canvas.width = img.width; canvas.height = img.height;
                    this.drawScene(img);
                };
                img.onerror = () => {
                    this.core.loadedImages[imagePath] = "failed";
                    this.drawScene(null);
                };
                img.src = `${this.core.serverUrl}/vault_media?filepath=${encodeURIComponent(imagePath)}`;
            } else {
                this.drawScene(null);
            }
        } else {
            this.drawScene(null);
        }
    }
}

class DMEngineClientCore extends EventEmitter {
    constructor(view, platform) {
        this.view = view;
        this.platform = platform; // "web" or "obsidian"

        // State
        this.activeCharacter = "Human DM";
        this.clientId = crypto.randomUUID();
        this.vaultPath = "";
        this.serverUrl = "http://127.0.0.1:8000";
        this.listenController = null;
        this.pollInterval = null;
        this.availableCharacters = new Set(["Human DM"]);
        this.lastUpdateCheck = 0;
        this.loadedImages = {};
        this.isMapDragging = false;
        this.isDrawingPath = false;
        this.waypoints = [];
        this.snapToGrid = JSON.parse(localStorage.getItem("dm_snap_to_grid") || "true");
        this.aoeMode = null;
        this.aoeSize = 20;
        this.mouseX = 0;
        this.mouseY = 0;
        this.currentMapData = null; // To store map data for event listeners
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

        if (this.platform === "obsidian") {
            this.serverUrl = window.localStorage.getItem("dm_server_url") || "http://127.0.0.1:8000";
            this.vaultPath = this.view.app.vault.adapter.getBasePath();
        } else if (this.platform === "web") {
            this.serverUrl = window.localStorage.getItem("dm_server_url_web") || "http://127.0.0.1:8000";
            this.vaultPath = window.localStorage.getItem("dm_vault_path") || "";
        }

        // Polyfill Obsidian's custom DOM helpers for the generic web browser
        if (this.platform === "web") {
            if (typeof HTMLElement !== "undefined" && !HTMLElement.prototype.createEl) {
                HTMLElement.prototype.empty = function () { this.innerHTML = ""; };
                HTMLElement.prototype.createEl = function (tag, opt) {
                    const el = document.createElement(tag);
                    if (opt) {
                        if (opt.cls) el.className = opt.cls;
                        if (opt.text) el.textContent = opt.text;
                        if (opt.type) el.type = opt.type;
                        if (opt.value) el.value = opt.value;
                        if (opt.name) el.name = opt.name;
                        if (opt.margin) el.style.margin = opt.margin;
                    }
                    this.appendChild(el);
                    return el;
                };
                HTMLElement.prototype.createDiv = function (opt) { return this.createEl('div', opt); };
                HTMLElement.prototype.createSpan = function (opt) { return this.createEl('span', opt); };
            }
        }

        // MapRenderer — owns Canvas 2D map rendering
        this.mapRenderer = new MapRenderer(this, this);
    }


    updatePerspectiveStyles() {
        const styleEl = document.getElementById('dm-perspective-styles');
        if (!styleEl) return;

        // Sync the dropdown options if it exists
        if (this.view && this.view.ui && this.view.ui.dmFilterContainer) {
            this.view.ui.dmFilterContainer.style.display = (this.activeCharacter === "Human DM") ? "flex" : "none";

            if (this.activeCharacter === "Human DM" && this.view.ui.dmFilterSelect) {
                const currentVal = this.view.ui.dmFilterSelect.value || "ALL_PLAYERS";
                this.view.ui.dmFilterSelect.innerHTML = `<option value="ALL_PLAYERS">All Players</option>`;
                this.availableCharacters.forEach(c => {
                    if (c !== "Human DM") {
                        const opt = document.createElement("option");
                        opt.value = c;
                        opt.text = c;
                        this.view.ui.dmFilterSelect.appendChild(opt);
                    }
                });
                this.view.ui.dmFilterSelect.value = currentVal;
                this.dmPerspectiveFilter = this.view.ui.dmFilterSelect.value;
            }
        }

        if (this.view && this.view.ui && this.view.ui.livePatchContainer) {
            this.view.ui.livePatchContainer.style.display = (this.activeCharacter === "Human DM") ? "flex" : "none";
        }

        let css = `
            .perspective { display: none; margin-bottom: 10px; padding: 10px; border-left: 3px solid var(--interactive-accent); background: var(--background-modifier-hover); border-radius: 4px; }
            .perspective[data-target="ALL"] { display: block; border-left: none; background: transparent; padding: 0; }
        `;

        if (this.activeCharacter === "Human DM") {
            if (this.dmPerspectiveFilter === "ALL_PLAYERS") {
                css += `.perspective { display: block; }`;
            } else {
                css += `.perspective[data-target="${this.dmPerspectiveFilter}"] { display: block; }`;
            }
            css += `
                .perspective:not([data-target="ALL"])::before {
                    content: "[Secret to " attr(data-target) "]:\\A";
                    white-space: pre-wrap;
                    font-weight: bold;
                    color: var(--text-accent);
                    display: block;
                    margin-bottom: 5px;
                }
            `;
        } else {
            css += `.perspective[data-target="${this.activeCharacter}"] { display: block; }`;
        }
        styleEl.textContent = css;
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
                })
            });

            if (response.ok) {
                const data = await response.json();
                this.updateRadioUI(data.locked_characters || []);
                this.setConnectionStatus(true);

                // Use party info from heartbeat (includes typing status)
                if (data.party && data.party.length > 0) {
                    this.renderPartySidebar(data.party);
                }

                // Use inlined character_sheet + map_state from heartbeat (avoids extra round-trips)
                if (data.character_sheet && !data.character_sheet.error) {
                    this.renderCharacterSheet(data.character_sheet);
                }
                if (data.map_state && data.map_state.map_data) {
                    this.currentMapData = data.map_state.map_data;
                    if (!this.isMapDragging) this.renderMaps(data.map_state);
                }

                // Check for updates every 60 seconds (DM only)
                const now = Date.now();
                if (now - this.lastUpdateCheck > 60000 && this.activeCharacter === "Human DM") {
                    this.lastUpdateCheck = now;
                    fetch(`${this.serverUrl}/check_updates`)
                        .then(r => r.json())
                        .then(data => {
                            if (this.view.ui && this.view.ui.updateBtn) {
                                this.view.ui.updateBtn.style.display = data.update_available ? "inline-block" : "none";
                            }
                        }).catch(() => { });
                }
            } else {
                this.setConnectionStatus(false);
            }
        } catch (e) {
            // Fail silently in the UI to prevent spamming, but log to console for debugging
            console.warn("DM Engine Heartbeat failed to connect:", e.message);
            this.setConnectionStatus(false);
        }
    }

    setConnectionStatus(isLive) {
        if (!this.view.ui.statusIndicator) return;
        if (isLive) {
            this.view.ui.statusIndicator.textContent = "🟢 Live";
            this.view.ui.statusIndicator.style.color = "var(--text-success, #28a745)";
        } else {
            this.view.ui.statusIndicator.textContent = "🔴 Disconnected";
            this.view.ui.statusIndicator.style.color = "var(--text-error, #dc3545)";
        }
    }

    async fetchPartyStatus() {
        try {
            const res = await fetch(`${this.serverUrl}/party_status`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath })
            });
            if (res.ok) {
                const data = await res.json();
                this.lastPartyData = data.party || [];
                this.renderPartySidebar(this.lastPartyData);
            }
        } catch (e) { }
    }

    renderPartySidebar(partyMembers) {
        if (!this.view.ui || !this.view.ui.partyList) return;
        const container = this.view.ui.partyList;
        container.innerHTML = "";

        // Merge typing status from activeTypers (SSE) with heartbeat data
        const typingFromSSE = this.activeTypers || new Set();

        // Group by location
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
            mapHeader.style.color = "var(--text-muted)";
            mapHeader.style.borderBottom = "1px solid var(--background-modifier-border)";
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

                // Dim if locked by another client
                if (m.locked_by_other) {
                    memberDiv.style.opacity = "0.6";
                }

                const topRow = document.createElement("div");
                topRow.style.display = "flex";
                topRow.style.justifyContent = "space-between";
                topRow.style.alignItems = "center";

                const nameSpan = document.createElement("span");
                nameSpan.style.fontWeight = "bold";
                nameSpan.textContent = m.name;
                if (m.locked_by_other) {
                    nameSpan.style.textDecoration = "line-through";
                    nameSpan.title = "In use by another player";
                }

                const statusSpan = document.createElement("span");
                statusSpan.style.fontSize = "1.2em";
                let statusHtml = m.is_online ? "<span title='Online'>🟢</span>" : "<span title='Offline' style='opacity:0.5'>🔴</span>";
                if (m.is_active) statusHtml += " <span title='Active recently'>⚡</span>";
                // Use typing from SSE (live) or from heartbeat data
                const isTyping = typingFromSSE.has(m.name) || m.is_typing;
                if (isTyping) statusHtml += " <span title='Typing...'>💬</span>";
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
                    hpBarFill.style.background = "var(--text-error)";
                } else if (hpPct <= 0.25) {
                    hpBarFill.style.background = "linear-gradient(90deg, #dc3545, #ff4d5a)";
                } else if (hpPct <= 0.5) {
                    hpBarFill.style.background = "linear-gradient(90deg, #e0a800, #f0c000)";
                } else {
                    hpBarFill.style.background = "linear-gradient(90deg, #28a745, #34ce57)";
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

    handleTyping() {
        const now = Date.now();
        if (now - this.lastTypedTime > 2000) { // Debounce sending to every 2 seconds
            this.lastTypedTime = now;
            fetch(`${this.serverUrl}/typing`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ client_id: this.clientId, character: this.activeCharacter, is_typing: true })
            }).catch(() => { });
        }

        if (this.typingTimeout) clearTimeout(this.typingTimeout);
        this.typingTimeout = setTimeout(() => {
            fetch(`${this.serverUrl}/typing`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ client_id: this.clientId, character: this.activeCharacter, is_typing: false })
            }).catch(() => { });
        }, 4000); // Expire if no keys pressed for 4 seconds
    }

    async fetchCharacters() {
        if (this.platform === "obsidian") return; // Handled dynamically via local plugin access
        try {
            const res = await fetch(`${this.serverUrl}/characters`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath })
            });
            if (res.ok) {
                const data = await res.json();
                this.availableCharacters = new Set(data.characters || ["Human DM"]);
                this.renderCharacterRadios([], this.availableCharacters);
            }
        } catch (e) { console.error("Failed to fetch characters:", e); }
    }

    async fetchCharacterSheet() {
        try {
            const res = await fetch(`${this.serverUrl}/character_sheet`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath, character: this.activeCharacter })
            });
            if (res.ok) {
                const data = await res.json();
                this.renderCharacterSheet(data);
            }
        } catch (e) { }
    }

    async fetchMaps() {
        if (this.isMapDragging) return; // Don't interrupt a drag with a background refresh
        try {
            const res = await fetch(`${this.serverUrl}/map_state`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath })
            });
            if (res.ok) {
                const data = await res.json();
                this.currentMapData = data.map_data; // Store map data
                if (!this.isMapDragging) this.renderMaps(data);
            }
        } catch (e) { }
    }

    renderCharacterSheet(data) {
        if (!data || data.error) {
            if (this.view.viewSheet) this.view.viewSheet.innerHTML = `<div style="color:var(--text-error);">${data ? data.error : "Failed to load sheet."}</div>`;
            return;
        }
        const s = data.sheet;
        const hp = s.hp !== undefined ? s.hp : "?";
        const maxHp = s.max_hp !== undefined ? s.max_hp : "?";

        // Handle conditions - could be array of strings or objects
        let condsHtml = "None";
        if (s.conditions && s.conditions.length > 0) {
            condsHtml = s.conditions.map(c => {
                const name = typeof c === 'string' ? c : c.name;
                return `<span style="background:var(--background-modifier-border); padding:2px 6px; border-radius:3px; margin-right:4px;">${name}</span>`;
            }).join("");
        }

        const equip = s.equipment ? Object.entries(s.equipment).map(([k, v]) => `<li><b>${k.replace(/_/g, ' ')}</b>: ${v}</li>`).join("") : "None";
        const res = s.resources ? Object.entries(s.resources).map(([k, v]) => `<li><b>${k}</b>: ${v}</li>`).join("") : "None";

        // Ability scores
        let abilitiesHtml = "";
        if (s.abilities && Object.keys(s.abilities).length > 0) {
            const abbr = { str: "STR", dex: "DEX", con: "CON", int: "INT", wis: "WIS", cha: "CHA" };
            abilitiesHtml = `<div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:10px;">
                ${Object.entries(s.abilities).map(([k, v]) => {
                    const mod = Math.floor((v - 10) / 2);
                    const sign = mod >= 0 ? "+" : "";
                    return `<div style="background:var(--background-modifier-form-field); padding:8px; border-radius:4px; text-align:center;">
                        <div style="font-size:0.8em; color:var(--text-muted);">${abbr[k] || k.toUpperCase()}</div>
                        <div style="font-size:1.3em; font-weight:bold;">${v}</div>
                        <div style="font-size:0.85em; color:var(--text-accent);">${sign}${mod}</div>
                    </div>`;
                }).join("")}
            </div>`;
        }

        // Speed
        const speed = s.speed || "30 ft";

        // Spell slots
        const spellSlots = s.spell_slots || null;
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
            </div>

            ${spellSlots ? `<p style="margin-top:10px;"><b>Spell Slots:</b> ${spellSlots}</p>` : ""}

            <h4 style="margin-bottom:5px; margin-top:15px;">Resources</h4>
            <ul style="margin-top:0;">${res}</ul>

            <h4 style="margin-bottom:5px;">Equipment</h4>
            <ul style="margin-top:0;">${equip}</ul>
        `;
    }

    renderMaps(data) {
        try {
            if (!this.view.viewMaps) return;
            this.view.viewMaps.innerHTML = "";
            if (!data || !data.map_data || (!data.map_data.walls.length && !data.map_data.dm_map_image_path)) {
                this.view.viewMaps.innerHTML = "<p style='color:var(--text-muted);'>No active maps loaded in engine.</p>";
                return;
            }

            const mapData = data.map_data;
            const entities = data.entities || [];
            const knownTraps = data.known_traps || [];
            const activePaths = data.active_paths || [];

            // --- NEW AOE TOOLBAR ---
            const mapToolbar = this.view.viewMaps.createDiv({ cls: "dm-map-toolbar" });
            mapToolbar.style.display = "flex";
            mapToolbar.style.gap = "10px";
            mapToolbar.style.padding = "5px";
            mapToolbar.style.marginBottom = "5px";
            mapToolbar.style.background = "var(--background-modifier-form-field)";
            mapToolbar.style.border = "1px solid var(--background-modifier-border)";
            mapToolbar.style.borderRadius = "4px";
            mapToolbar.style.alignItems = "center";
            mapToolbar.style.flexWrap = "wrap";

            mapToolbar.createSpan({ text: "🎯 AoE Template:", style: "font-weight: bold;" });

            const aoeShapeSelect = mapToolbar.createEl("select");
            ["None", "Circle", "Cone", "Line", "Cube"].forEach(s => {
                const opt = aoeShapeSelect.createEl("option", { text: s, value: s.toLowerCase() });
                if (this.aoeMode === s.toLowerCase()) opt.selected = true;
            });

            mapToolbar.createSpan({ text: "Size (ft):" });
            const sizeInput = mapToolbar.createEl("input", { type: "number", value: this.aoeSize.toString() });
            sizeInput.style.width = "60px";

            mapToolbar.createSpan({ text: "(Shift+Click map to Ping)", style: "font-size: 0.85em; color: var(--text-muted); margin-left: auto;" });

            const canvasContainer = this.view.viewMaps.createDiv();
            canvasContainer.style.flex = "1 1 auto";
            canvasContainer.style.overflow = "auto";
            canvasContainer.style.position = "relative";

            let imagePath = null;
            if (this.activeCharacter === "Human DM") {
                imagePath = mapData.dm_map_image_path || mapData.player_map_image_path;
            } else {
                imagePath = mapData.player_map_image_path || mapData.dm_map_image_path;
            }

            // Canvas sizing: use image dimensions if available, else use map data dimensions
            // SCALE: pixels per foot (15 is standard, gives 75px per 5ft square)
            const SCALE = 15;
            const mapWidth = mapData.width || 200;
            const mapHeight = mapData.height || 200;

            const canvas = document.createElement('canvas');
            canvas.width = mapWidth * SCALE;
            canvas.height = mapHeight * SCALE;
            canvas.style.backgroundColor = "var(--background-modifier-form-field)";
            canvas.style.borderRadius = "4px";
            canvasContainer.appendChild(canvas);

            // Hand off canvas + state to MapRenderer; handles image loading + initial draw
            if (imagePath && this.loadedImages[imagePath] instanceof Image) {
                canvas.width = this.loadedImages[imagePath].width;
                canvas.height = this.loadedImages[imagePath].height;
                this.mapRenderer.beginRender({ mapData, entities, knownTraps, activePaths, canvas, imagePath });
            } else {
                this.mapRenderer.beginRender({ mapData, entities, knownTraps, activePaths, canvas, imagePath: null });
            }

            aoeShapeSelect.addEventListener("change", (e) => {
                this.aoeMode = e.target.value === "none" ? null : e.target.value;
                this.mapRenderer.drawScene();
            });

            sizeInput.addEventListener("change", (e) => {
                this.aoeSize = parseInt(e.target.value) || 20;
                this.mapRenderer.drawScene();
            });

            // --- DRAG AND DROP LOGIC ---
            canvas.addEventListener('mousedown', (e) => {
                if (e.shiftKey) {
                    const rect = canvas.getBoundingClientRect();
                    let newX = ((e.clientX - rect.left) * (canvas.width / rect.width)) / SCALE;
                    let newY = ((e.clientY - rect.top) * (canvas.height / rect.width)) / SCALE;

                    if (this.snapToGrid && this.currentMapData) {
                        const gridSize = this.currentMapData.grid_scale;
                        newX = Math.round(newX / gridSize) * gridSize;
                        newY = Math.round(newY / gridSize) * gridSize;
                    }

                    fetch(`${this.serverUrl}/ping`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            client_id: this.clientId,
                            character: this.activeCharacter,
                            vault_path: this.vaultPath,
                            x: Math.round(newX * 10) / 10,
                            y: Math.round(newY * 10) / 10
                        })
                    }).catch(err => console.error("Ping error:", err));
                    return;
                }

                if (this.aoeMode) {
                    let text = "";
                    const size = this.aoeSize;
                    const x = Math.round(this.mouseX * 10) / 10;
                    const y = Math.round(this.mouseY * 10) / 10;

                    const hitEntities = [];
                    entities.forEach(ent => {
                        if (ent.hp <= 0) return;
                        const dx = ent.x - x;
                        const dy = ent.y - y;
                        const dist = Math.hypot(dx, dy);
                        const r = (ent.size / 2 || 2.5);

                        if (this.aoeMode === "circle") {
                            if (dist <= size + r) hitEntities.push(ent.name);
                        } else if (this.aoeMode === "cube") {
                            const half = size / 2 + r;
                            if (Math.abs(dx) <= half && Math.abs(dy) <= half) hitEntities.push(ent.name);
                        } else if (this.aoeMode === "cone" || this.aoeMode === "line") {
                            const activeEnt = entities.find(ev => ev.name === this.activeCharacter);
                            if (!activeEnt) return;
                            const edx = ent.x - activeEnt.x;
                            const edy = ent.y - activeEnt.y;
                            const edist = Math.hypot(edx, edy);
                            const eAngle = Math.atan2(edy, edx);
                            const castAngle = Math.atan2(y - activeEnt.y, x - activeEnt.x);

                            if (edist <= size + r) {
                                if (this.aoeMode === "cone") {
                                    let angleDiff = Math.abs(eAngle - castAngle);
                                    if (angleDiff > Math.PI) angleDiff = 2 * Math.PI - angleDiff;
                                    if (angleDiff <= Math.PI / 6 + 0.1) hitEntities.push(ent.name);
                                } else if (this.aoeMode === "line") {
                                    const perpDist = Math.abs((edx) * Math.sin(castAngle) - (edy) * Math.cos(castAngle));
                                    if (perpDist <= 2.5 + r) hitEntities.push(ent.name);
                                }
                            }
                        }
                    });

                    if (this.aoeMode === "circle" || this.aoeMode === "cube") {
                        text = `I center a ${size}ft ${this.aoeMode} at coordinates (${x}, ${y}).`;
                    } else {
                        text = `I project a ${size}ft ${this.aoeMode} towards coordinates (${x}, ${y}).`;
                    }

                    text += hitEntities.length > 0 ? ` This hits: ${hitEntities.join(", ")}.` : ` This hits no one.`;

                    this.view.ui.chatInput.value = this.view.ui.chatInput.value ? this.view.ui.chatInput.value + "\n" + text : text;

                    document.querySelectorAll(".dm-tab-bar button")[0].click();
                    this.view.ui.chatInput.focus();

                    this.aoeMode = null;
                    if (aoeShapeSelect) aoeShapeSelect.value = "none";
                    this.mapRenderer.drawScene();
                    return;
                }

                const rect = canvas.getBoundingClientRect();
                const mouseX = (e.clientX - rect.left) * (canvas.width / rect.width);
                const mouseY = (e.clientY - rect.top) * (canvas.height / rect.height);

                // Check collision backwards (top-most drawn entity selected first)
                for (let i = entities.length - 1; i >= 0; i--) {
                    const ent = entities[i];
                    if (ent.hp <= 0) continue;
                    if (Math.hypot(mouseX - (ent.x * SCALE), mouseY - (ent.y * SCALE)) <= (ent.size / 2) * SCALE) {
                        if (this.activeCharacter === "Human DM" || ent.name === this.activeCharacter) {
                            this.isMapDragging = true;
                            canvas.draggedEntity = ent;
                            canvas.dragStartX = ent.x;
                            canvas.dragStartY = ent.y;
                            break;
                        }
                    }
                }
            });

            canvas.addEventListener('mousemove', (e) => {
                const rect = canvas.getBoundingClientRect();
                let newX = ((e.clientX - rect.left) * (canvas.width / rect.width)) / SCALE;
                let newY = ((e.clientY - rect.top) * (canvas.height / rect.width)) / SCALE;

                if (this.snapToGrid && this.currentMapData) {
                    const gridSize = this.currentMapData.grid_scale; // e.g., 5.0
                    newX = Math.round(newX / gridSize) * gridSize;
                    newY = Math.round(newY / gridSize) * gridSize;
                }

                this.mouseX = newX;
                this.mouseY = newY;

                if (this.aoeMode) {
                    this.mapRenderer.drawScene();
                } else if (this.isMapDragging && canvas.draggedEntity) {
                    canvas.draggedEntity.x = newX;
                    canvas.draggedEntity.y = newY;
                    this.mapRenderer.drawScene(); // Live re-render
                }
            });

            const stopDrag = async () => {
                if (this.isMapDragging && canvas.draggedEntity) {
                    const ent = canvas.draggedEntity;
                    this.isMapDragging = false;
                    canvas.draggedEntity = null;

                    if (this.activeCharacter === "Human DM") {
                        try {
                            await fetch(`${this.serverUrl}/ooc_move_entity`, {
                                method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ vault_path: this.vaultPath, entity_name: ent.name, x: ent.x, y: ent.y })
                            });
                        } catch (err) { console.error("Failed to move entity", err); }
                    } else {
                        const payload = {
                            vault_path: this.vaultPath,
                            entity_name: ent.name,
                            waypoints: [[canvas.dragStartX, canvas.dragStartY], [ent.x, ent.y]],
                            force_execute: false
                        };

                        try {
                            const res = await fetch(`${this.serverUrl}/propose_move`, {
                                method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(payload)
                            });

                            if (res.ok) {
                                const processResponse = async (data) => {
                                    if (!data.is_valid) {
                                        let msg = `Move Invalid: ${data.invalid_reason}`;
                                        let takeAlt = false;
                                        if (data.alternative_path && data.alternative_path.length > 0) {
                                            msg += `\n\nAn alternative route is shown in yellow. Take it instead?`;
                                            takeAlt = confirm(msg);
                                        } else {
                                            new Notice(msg);
                                        }

                                        if (takeAlt) {
                                            payload.waypoints = data.alternative_path;
                                            payload.force_execute = true;
                                            const altRes = await fetch(`${this.serverUrl}/propose_move`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
                                            if (altRes.ok) processResponse(await altRes.json());
                                        } else {
                                            ent.x = canvas.dragStartX; ent.y = canvas.dragStartY;
                                            await fetch(`${this.serverUrl}/clear_path`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entity_name: ent.name, vault_path: this.vaultPath }) });
                                            this.fetchMaps();
                                        }
                                    } else if (!data.executed) {
                                        let msg = "⚠️ WARNING: This move will trigger:\n";
                                        if (data.opportunity_attacks.length > 0) msg += `- Opportunity Attacks from: ${data.opportunity_attacks.join(', ')}\n`;
                                        if (data.traps_triggered.length > 0) msg += `- Known Traps: ${data.traps_triggered.join(', ')}\n`;
                                        msg += "\nDo you want to proceed anyway?";

                                        if (confirm(msg)) {
                                            payload.force_execute = true;
                                            const fRes = await fetch(`${this.serverUrl}/propose_move`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
                                            if (fRes.ok) processResponse(await fRes.json());
                                        } else {
                                            ent.x = canvas.dragStartX; ent.y = canvas.dragStartY;
                                            await fetch(`${this.serverUrl}/clear_path`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entity_name: ent.name, vault_path: this.vaultPath }) });
                                            this.fetchMaps();
                                        }
                                    } else {
                                        ent.x = data.final_x;
                                        ent.y = data.final_y;
                                        this.fetchMaps();
                                    }
                                };

                                await this.fetchMaps();
                                setTimeout(async () => {
                                    const data = await res.json();
                                    processResponse(data);
                                }, 50);
                            }
                        } catch (err) {
                            ent.x = canvas.dragStartX; ent.y = canvas.dragStartY;
                            this.mapRenderer.drawScene();
                        }
                    }
                }
            };

            canvas.addEventListener('mouseup', stopDrag);
            canvas.addEventListener('mouseout', stopDrag);
        } catch (error) {
            console.error("Engine Render Error:", error);
            if (this.view.viewMaps) this.view.viewMaps.innerHTML = `<p style='color:var(--text-error);'>Error rendering map: ${error.message}</p>`;
        }
    }

    async startListening() {
        const MAX_DELAY_MS = 30000;
        let delayMs = 1000;
        while (!this.listenController.signal.aborted) {
            if (this.listenController) {
                this.listenController.abort();
            }
            this.listenController = new AbortController();
            try {
                const response = await fetch(`${this.serverUrl}/listen?client_id=${this.clientId}`, {
                    method: "GET",
                    signal: this.listenController.signal
                });

                if (!response.ok) {
                    delayMs = Math.min(delayMs * 2, MAX_DELAY_MS);
                    await new Promise(r => setTimeout(r, delayMs));
                    continue;
                }

                delayMs = 1000; // reset backoff on successful connection

                const reader = response.body.getReader();
                const decoder = new TextDecoder("utf-8");
                let buffer = "";
                let msgDiv = null;
                let contentDiv = null;
                let accumulatedText = "";
                let lastSSEActivity = Date.now();

                while (!this.listenController.signal.aborted) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    lastSSEActivity = Date.now();
                    buffer += decoder.decode(value, { stream: true });
                    const parts = buffer.split("\n\n");
                    buffer = parts.pop();

                    let needsRender = false;
                    for (const part of parts) {
                        if (part.startsWith("data: ")) {
                            try {
                                const data = JSON.parse(part.substring(6));

                                if (data.type === "ping") {
                                    this.mapRenderer.activePings = this.mapRenderer.activePings || [];
                                    this.mapRenderer.activePings.push({ x: data.x, y: data.y, character: data.character, time: Date.now() });
                                    if (!this.mapRenderer.pingAnimationId) this.mapRenderer.animatePings();
                                }

                                if (data.type === "typing") {
                                    if (data.is_typing) {
                                        this.activeTypers.add(data.character);
                                    } else {
                                        this.activeTypers.delete(data.character);
                                    }
                                    this.renderPartySidebar(this.lastPartyData);
                                }

                                if (data.status === "streaming" || data.status === "error") {
                                    if (data.reply) {
                                        if (!msgDiv) {
                                            msgDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-message" });
                                            msgDiv.style.marginBottom = "15px";
                                            msgDiv.style.lineHeight = "1.5";
                                            const senderSpan = msgDiv.createSpan({ text: `DM (Broadcast): ` });
                                            senderSpan.style.fontWeight = "bold";
                                            senderSpan.style.color = "var(--text-muted)";
                                            contentDiv = msgDiv.createDiv({ cls: "dm-message-content" });
                                            contentDiv.style.marginTop = "5px";
                                        }
                                        accumulatedText += data.reply;
                                        needsRender = true;
                                    }
                                } else if (data.status === "done") {
                                    msgDiv = null;
                                    contentDiv = null;
                                    accumulatedText = "";
                                }
                            } catch (e) {
                                console.error("Error parsing JSON chunk:", e, part);
                            }
                        }
                    }

                    if (needsRender && contentDiv) {
                        contentDiv.empty();
                        await MarkdownRenderer.renderMarkdown(accumulatedText, contentDiv, "", this.view);
                        const paragraphs = contentDiv.querySelectorAll("p");
                        paragraphs.forEach(p => { p.style.marginTop = "0"; p.style.marginBottom = "0.5em"; });
                        this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;
                        this._gcChatHistory();
                    }
                }
                // Fallback poll: if SSE stream closed and we've been disconnected >10s, refresh state
                if (!this.listenController.signal.aborted && Date.now() - lastSSEActivity > 10000) {
                    this.syncState();
                }
            } catch (e) {
                if (e.name !== "AbortError") {
                    console.error("Listen Error:", e);
                    new Notice("Listen stream disconnected. Reconnecting...");
                    delayMs = Math.min(delayMs * 2, MAX_DELAY_MS);
                    await new Promise(r => setTimeout(r, delayMs));
                }
            }
        }
    }

    // Remove oldest .dm-message elements when count exceeds limit
    _gcChatHistory() {
        const MAX_MESSAGES = 200;
        const chatHistory = this.view.ui.chatHistory;
        if (!chatHistory) return;
        const messages = chatHistory.querySelectorAll(".dm-message");
        while (messages.length > MAX_MESSAGES) {
            messages[0].remove();
            messages.shift();
        }
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
                })
            });
            if (!res.ok) return;
            const data = await res.json();

            const isCrit = data.is_crit;
            const isFumble = data.is_fumble;
            const rollColor = isCrit ? "#ffd700" : isFumble ? "#dc3545" : "var(--text-accent)";
            const rollBorder = isCrit ? "1px solid #ffd700" : isFumble ? "1px solid #dc3545" : "1px solid var(--background-modifier-border)";
            const rollBg = isCrit ? "rgba(255,215,0,0.08)" : isFumble ? "rgba(220,53,69,0.08)" : "var(--background-modifier-form-field)";
            const rollLabel = isCrit ? "🎯 CRIT!" : isFumble ? "💀 FUMBLE" : "🎲";

            const modStr = data.modifier_op
                ? ` ${data.modifier_op}${data.modifier === 0 ? "" : data.modifier}`
                : "";
            const totalStr = `${data.total}`;
            const rollsStr = data.rolls.length > 1
                ? `[${data.roll_str}]`
                : `[${data.roll_str}]`;

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

    async submitMessage() {
        if (!this.view.ui || !this.view.ui.chatInput) return;
        const text = this.view.ui.chatInput.value.trim();
        if (!text) return;

        if (text.startsWith(">") && this.activeCharacter !== "Human DM") {
            new Notice("Only the 'Human DM' is allowed to execute OOC commands (>).");
            this.appendMessage("System", "Only the 'Human DM' is allowed to execute OOC commands (>).", "red");
            this.view.ui.chatInput.value = "";
            return;
        }

        this.view.ui.chatInput.value = "";
        this.view.ui.chatInput.style.height = "auto";
        this.view.ui.chatInput.style.height = "40px";
        this.view.ui.chatInput.disabled = true;

        if (this.typingTimeout) clearTimeout(this.typingTimeout);
        // Tell server immediately we stopped typing
        fetch(`${this.serverUrl}/typing`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ client_id: this.clientId, character: this.activeCharacter, is_typing: false })
        }).catch(() => { });

        this.appendMessage(this.activeCharacter, text, "var(--text-accent)");

        const loadingDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-loading" });
        loadingDiv.style.fontStyle = "italic";
        loadingDiv.style.color = "var(--text-muted)";
        loadingDiv.style.marginTop = "10px";
        loadingDiv.innerHTML = "🎲 DM is thinking...";
        this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;

        try {
            const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const response = await fetch(`${this.serverUrl}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: text,
                    character: this.activeCharacter,
                    vault_path: this.vaultPath,
                    client_id: this.clientId,
                    roll_automations: this.rollAutomations,
                    request_id: requestId,
                    protocol_version: 2,
                })
            });

            loadingDiv.remove();

            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`HTTP error! status: ${response.status} - ${errorText}`);
            }

            const msgDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-message" });
            msgDiv.style.marginBottom = "15px";
            msgDiv.style.lineHeight = "1.5";

            const senderSpan = msgDiv.createSpan({ text: `DM: ` });
            senderSpan.style.fontWeight = "bold";
            senderSpan.style.color = "var(--text-normal)";

            const contentDiv = msgDiv.createDiv({ cls: "dm-message-content" });
            contentDiv.style.marginTop = "5px";

            let accumulatedText = "";
            const reader = response.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const parts = buffer.split("\n\n");
                buffer = parts.pop();

                let needsRender = false;
                for (const part of parts) {
                    if (part.startsWith("data: ")) {
                        try {
                            const data = JSON.parse(part.substring(6));
                            if (data.status === "streaming" || data.status === "error") {
                                accumulatedText += data.reply;
                                needsRender = true;
                            }
                        } catch (e) {
                            console.error("Error parsing JSON chunk:", e, part);
                        }
                    }
                }

                if (needsRender) {
                    contentDiv.empty();
                    await MarkdownRenderer.renderMarkdown(accumulatedText, contentDiv, "", this.view);

                    const paragraphs = contentDiv.querySelectorAll("p");
                    paragraphs.forEach(p => {
                        p.style.marginTop = "0";
                        p.style.marginBottom = "0.5em";
                    });

                    this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;
                }
            }
            this._gcChatHistory();

        } catch (error) {
            loadingDiv.remove();
            console.error("Network/Stream Error:", error);
            new Notice("Failed to reach DM Engine or stream was interrupted.");
            this.appendMessage("System", `**Error:** ${error.message}`, "red");
        } finally {
            this.view.ui.chatInput.disabled = false;
            this.view.ui.chatInput.focus();
        }
    }

    async appendMessage(sender, text, color) {
        const msgDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-message" });
        msgDiv.style.marginBottom = "15px";
        msgDiv.style.lineHeight = "1.5";

        const senderSpan = msgDiv.createSpan({ text: `${sender}: ` });
        senderSpan.style.fontWeight = "bold";
        senderSpan.style.color = color;

        const contentDiv = msgDiv.createDiv({ cls: "dm-message-content" });
        contentDiv.style.marginTop = "5px";

        await MarkdownRenderer.renderMarkdown(text, contentDiv, "", this.view);

        const paragraphs = contentDiv.querySelectorAll("p");
        paragraphs.forEach(p => {
            p.style.marginTop = "0";
            p.style.marginBottom = "0.5em";
        });

        this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;
        this._gcChatHistory();
    }

    updateRadioUI(lockedCharacters) {
        const chars = new Set(["Human DM"]);
        if (this.platform === "obsidian") {
            const files = this.view.app.vault.getMarkdownFiles();
            for (const file of files) {
                const cache = this.view.app.metadataCache.getFileCache(file);
                if (cache && cache.frontmatter && cache.frontmatter.tags) {
                    const tags = Array.isArray(cache.frontmatter.tags) ? cache.frontmatter.tags : [cache.frontmatter.tags];
                    if (tags.some(t => String(t).toLowerCase() === 'pc' || String(t).toLowerCase() === 'player' || String(t).toLowerCase() === 'party_npc')) {
                        chars.add(cache.frontmatter.name || file.basename);
                    }
                }
            }
        } else if (this.platform === "web") {
            this.availableCharacters.forEach(c => chars.add(c));
        }

        const existingRadios = Array.from(this.view.ui.charSelect.querySelectorAll('input[type="radio"]'));
        const existingValues = new Set(existingRadios.map(r => r.value));

        let listChanged = chars.size !== existingValues.size;
        if (!listChanged) {
            for (const c of chars) {
                if (!existingValues.has(c)) { listChanged = true; break; }
            }
        }

        if (listChanged) {
            this.renderCharacterRadios(lockedCharacters, chars);
        } else {
            for (const radio of existingRadios) {
                const lbl = radio.parentElement;
                if (radio.value !== "Human DM" && lockedCharacters.includes(radio.value)) {
                    radio.disabled = true;
                    lbl.style.opacity = "0.5";
                    lbl.title = "Character is controlled by another player.";
                } else {
                    radio.disabled = false;
                    lbl.style.opacity = "1";
                    lbl.title = "";
                }
            }
        }
    }

    renderCharacterRadios(lockedCharacters = [], precalculatedChars = null) {
        this.view.ui.charSelect.empty();

        let chars = precalculatedChars;
        if (!chars) {
            chars = new Set(["Human DM"]);
            if (this.platform === "obsidian") {
                const files = this.view.app.vault.getMarkdownFiles();
                for (const file of files) {
                    const cache = this.view.app.metadataCache.getFileCache(file);
                    if (cache && cache.frontmatter && cache.frontmatter.tags) {
                        const tags = Array.isArray(cache.frontmatter.tags) ? cache.frontmatter.tags : [cache.frontmatter.tags];
                        if (tags.some(t => String(t).toLowerCase() === 'pc' || String(t).toLowerCase() === 'player' || String(t).toLowerCase() === 'party_npc')) {
                            chars.add(cache.frontmatter.name || file.basename);
                        }
                    }
                }
            }
        } else if (this.platform === "web") {
            chars = this.availableCharacters;
        }

        if (!chars.has(this.activeCharacter)) {
            this.activeCharacter = "Human DM";
        }

        for (const char of chars) {
            const lbl = this.view.ui.charSelect.createEl("label");
            lbl.style.display = "flex";
            lbl.style.alignItems = "center";
            lbl.style.gap = "4px";
            lbl.style.cursor = "pointer";

            const radio = lbl.createEl("input", { type: "radio", name: "dm-char-select", value: char });
            if (char === this.activeCharacter) radio.checked = true;

            if (char !== "Human DM" && lockedCharacters.includes(char)) {
                radio.disabled = true;
                lbl.style.opacity = "0.5";
                lbl.title = "Character is controlled by another player.";
            }

            lbl.appendChild(document.createTextNode(char));

            radio.addEventListener("change", async (e) => {
                if (e.target.checked) {
                    const newChar = e.target.value;
                    try {
                        const response = await fetch(`${this.serverUrl}/switch_character`, {
                            method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ old_character: this.activeCharacter, new_character: newChar, client_id: this.clientId })
                        });

                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.detail || "Lock denied");
                        }

                        this.activeCharacter = newChar;
                        this.updatePerspectiveStyles();
                        if (this.view.ui.chatInput) this.view.ui.chatInput.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do? (Shift+Enter for new line)`;
                        this.appendMessage("System", `Active character switched to: **${this.activeCharacter}**`, "var(--text-faint)");

                        this.syncState();
                    } catch (err) {
                        this.appendMessage("System", `**Error swapping character:** ${err.message}`, "red");
                        const radios = Array.from(this.view.ui.charSelect.querySelectorAll('input[type="radio"]'));
                        const oldRadio = radios.find(r => r.value === this.activeCharacter);
                        if (oldRadio) oldRadio.checked = true;
                    }
                }
            });
        }

        if (this.view.ui.chatInput) this.view.ui.chatInput.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do? (Shift+Enter for new line)`;
    }
}

class DMChatView extends ItemView {
    constructor(leaf) {
        super(leaf);
        this.clientCore = new DMEngineClientCore(this, "obsidian");
    }

    getViewType() {
        return VIEW_TYPE_DM_CHAT;
    }

    getDisplayText() {
        return "DM Engine";
    }

    async onOpen() {
        this.ui = {};
        const container = this.containerEl.children[1];
        container.empty();
        container.addClass('dm-chat-wrapper');

        // 👇 1. Make the main wrapper a Flex Column fixed to 100% height
        container.style.display = "flex";
        container.style.flexDirection = "column";
        container.style.height = "100%";
        container.style.overflow = "hidden"; // This kills the ugly outer scrollbar!

        // Main layout wrapper to support horizontal sidebar
        const layoutWrapper = container.createDiv();
        layoutWrapper.style.display = "flex";
        layoutWrapper.style.flexDirection = "row";
        layoutWrapper.style.height = "100%";
        layoutWrapper.style.width = "100%";

        const sidebar = layoutWrapper.createDiv({ cls: "dm-party-sidebar" });
        sidebar.style.flex = "0 0 220px";
        sidebar.style.borderRight = "1px solid var(--background-modifier-border)";
        sidebar.style.padding = "10px";
        sidebar.style.overflowY = "auto";
        sidebar.style.backgroundColor = "var(--background-secondary)";
        sidebar.createEl("h3", { text: "Party", margin: "0" }).style.marginTop = "0";

        this.ui.partyList = sidebar.createDiv();
        this.ui.partyList.style.marginTop = "10px";
        this.ui.partyList.textContent = "Waiting for data...";

        const mainArea = layoutWrapper.createDiv();
        mainArea.style.flex = "1";
        mainArea.style.display = "flex";
        mainArea.style.flexDirection = "column";
        mainArea.style.overflow = "hidden";

        // Top Control Bar
        const topBar = mainArea.createDiv();
        topBar.style.display = "flex";
        topBar.style.justifyContent = "space-between";
        topBar.style.alignItems = "center";
        topBar.style.flex = "0 0 auto";
        topBar.style.padding = "5px";
        topBar.style.borderBottom = "1px solid var(--background-modifier-border)";

        const titleContainer = topBar.createDiv();
        titleContainer.style.display = "flex";
        titleContainer.style.alignItems = "center";
        titleContainer.style.gap = "8px";

        const header = titleContainer.createEl("h4", { text: "DM Engine", margin: "0" });
        header.style.margin = "0";

        this.ui.statusIndicator = titleContainer.createSpan({ text: "🟡 Checking..." });
        this.ui.statusIndicator.style.fontSize = "0.8em";
        this.ui.statusIndicator.style.fontWeight = "600";

        this.ui.updateBtn = titleContainer.createEl("button", { text: "🔄 Update Available" });
        this.ui.updateBtn.style.display = "none";
        this.ui.updateBtn.style.fontSize = "0.75em";
        this.ui.updateBtn.style.padding = "2px 8px";
        this.ui.updateBtn.style.backgroundColor = "var(--interactive-accent)";
        this.ui.updateBtn.style.color = "var(--text-on-accent)";
        this.ui.updateBtn.style.border = "none";
        this.ui.updateBtn.style.cursor = "pointer";
        this.ui.updateBtn.addEventListener("click", async () => {
            this.ui.updateBtn.textContent = "Pulling...";
            this.ui.updateBtn.disabled = true;
            try {
                const res = await fetch(`${this.clientCore.serverUrl}/apply_update`, { method: "POST" });
                const data = await res.json();
                if (data.status === "success") {
                    new Notice("Update applied! Server is hot-reloading.");
                    this.ui.updateBtn.style.display = "none";
                } else { new Notice("Failed to pull update: " + data.message); }
            } catch (e) { new Notice("Error applying update."); }
            this.ui.updateBtn.textContent = "🔄 Update Available";
            this.ui.updateBtn.disabled = false;
        });

        // Listen Checkbox
        const listenLabel = topBar.createEl("label");
        listenLabel.style.display = "flex";
        listenLabel.style.alignItems = "center";
        listenLabel.style.gap = "5px";
        listenLabel.style.cursor = "pointer";
        const listenCheckbox = listenLabel.createEl("input", { type: "checkbox" });
        listenLabel.appendChild(document.createTextNode("Listen"));

        listenCheckbox.addEventListener("change", (e) => {
            if (e.target.checked) {
                this.clientCore.startListening();
            } else {
                if (this.clientCore.listenController) {
                    this.clientCore.listenController.abort();
                    this.clientCore.listenController = null;
                }
            }
        });

        // Character Select Container
        this.ui.charSelect = mainArea.createDiv();
        this.ui.charSelect.style.flex = "0 0 auto";
        this.ui.charSelect.style.padding = "5px 10px";
        this.ui.charSelect.style.display = "flex";
        this.ui.charSelect.style.flexWrap = "wrap";
        this.ui.charSelect.style.gap = "10px";
        this.ui.charSelect.style.borderBottom = "1px solid var(--background-modifier-border)";

        // Collapsible Settings Panel
        const settingsWrapper = mainArea.createDiv({ cls: "dm-settings-wrapper" });
        settingsWrapper.style.flex = "0 0 auto";
        settingsWrapper.style.padding = "5px 10px";
        settingsWrapper.style.borderBottom = "1px solid var(--background-modifier-border)";

        const settingsHeader = settingsWrapper.createEl("div", { cls: "dm-settings-header" });
        settingsHeader.style.cursor = "pointer";
        settingsHeader.style.fontWeight = "bold";
        settingsHeader.style.display = "flex";
        settingsHeader.style.alignItems = "center";
        settingsHeader.style.fontSize = "0.9em";
        settingsHeader.style.color = "var(--text-muted)";
        settingsHeader.innerHTML = `<span class="dm-settings-icon" style="margin-right: 5px;">▶</span> Player Roll Automations`;

        const settingsContent = settingsWrapper.createDiv({ cls: "dm-settings-content" });
        settingsContent.style.display = "none";
        settingsContent.style.flexDirection = "column";
        settingsContent.style.gap = "5px";
        settingsContent.style.marginTop = "10px";
        settingsContent.style.fontSize = "0.85em";

        settingsHeader.addEventListener("click", () => {
            const isHidden = settingsContent.style.display === "none";
            settingsContent.style.display = isHidden ? "flex" : "none";
            settingsHeader.querySelector(".dm-settings-icon").innerText = isHidden ? "▼" : "▶";
        });

        // Server URL Setting
        const urlLabel = settingsContent.createEl("label");
        urlLabel.style.display = "flex";
        urlLabel.style.flexDirection = "column";
        urlLabel.style.gap = "4px";
        urlLabel.style.marginBottom = "5px";
        urlLabel.style.paddingBottom = "5px";
        urlLabel.style.borderBottom = "1px solid var(--background-modifier-border)";
        urlLabel.appendChild(document.createTextNode("Server Base URL:"));

        const urlInput = urlLabel.createEl("input", { type: "text" });
        urlInput.value = this.clientCore.serverUrl;
        urlInput.addEventListener("change", (e) => {
            this.clientCore.serverUrl = e.target.value.trim().replace(/\/+$/, "");
            window.localStorage.setItem("dm_server_url", this.clientCore.serverUrl);
            this.clientCore.syncState();
        });

        const createToggle = (key, labelText) => {
            const lbl = settingsContent.createEl("label");
            lbl.style.display = "flex";
            lbl.style.alignItems = "center";
            lbl.style.gap = "8px";
            lbl.style.cursor = "pointer";
            const cb = lbl.createEl("input", { type: "checkbox" });
            cb.checked = this.clientCore.rollAutomations[key];
            cb.addEventListener("change", (e) => {
                this.clientCore.rollAutomations[key] = e.target.checked;
                this.clientCore.syncState();
            });
            lbl.appendChild(document.createTextNode(labelText));
        };

        createToggle("hidden_rolls", "Automate Hidden Rolls");
        createToggle("saving_throws", "Automate Saving Throws");
        createToggle("skill_checks", "Automate Skill Checks");
        createToggle("attack_rolls", "Automate Attack Rolls");

        // New toggle for Snap to Grid
        const snapGridLabel = settingsContent.createEl("label");
        snapGridLabel.style.display = "flex";
        snapGridLabel.style.alignItems = "center";
        snapGridLabel.style.gap = "8px";
        snapGridLabel.style.cursor = "pointer";
        const snapGridCheckbox = snapGridLabel.createEl("input", { type: "checkbox" });
        snapGridCheckbox.checked = this.clientCore.snapToGrid;
        snapGridCheckbox.addEventListener("change", (e) => {
            this.clientCore.snapToGrid = e.target.checked;
            localStorage.setItem("dm_snap_to_grid", e.target.checked);
        });
        snapGridLabel.appendChild(document.createTextNode("Snap to 5ft Grid"));

        // Live Patch Toggle (DM Only)
        this.ui.livePatchContainer = settingsContent.createEl("label");
        this.ui.livePatchContainer.style.display = "none";
        this.ui.livePatchContainer.style.alignItems = "center";
        this.ui.livePatchContainer.style.gap = "8px";
        this.ui.livePatchContainer.style.cursor = "pointer";
        this.ui.livePatchContainer.style.color = "var(--text-error)";
        this.ui.livePatchCheckbox = this.ui.livePatchContainer.createEl("input", { type: "checkbox" });
        this.ui.livePatchContainer.appendChild(document.createTextNode("🔥 Enable AI Live Patching (Danger)"));
        this.ui.livePatchCheckbox.addEventListener("change", async (e) => {
            try {
                await fetch(`${this.clientCore.serverUrl}/toggle_live_patch`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ client_id: this.clientCore.clientId, character: this.clientCore.activeCharacter, enabled: e.target.checked })
                });
            } catch (err) { console.error(err); }
        });

        // Tab Bar
        const tabBar = mainArea.createDiv({ cls: "dm-tab-bar" });
        tabBar.style.display = "flex";
        tabBar.style.flex = "0 0 auto";
        tabBar.style.borderBottom = "1px solid var(--background-modifier-border)";
        tabBar.style.backgroundColor = "var(--background-secondary)";

        const btnChat = tabBar.createEl("button", { text: "💬 Chat" });
        const btnSheet = tabBar.createEl("button", { text: "📜 Sheet" });
        const btnMaps = tabBar.createEl("button", { text: "🗺️ Maps" });

        [btnChat, btnSheet, btnMaps].forEach(btn => {
            btn.style.flex = "1";
            btn.style.borderRadius = "0";
            btn.style.background = "transparent";
            btn.style.boxShadow = "none";
            btn.style.border = "none";
            btn.style.borderBottom = "2px solid transparent";
            btn.style.cursor = "pointer";
        });
        btnChat.style.borderBottom = "2px solid var(--interactive-accent)";

        // Views Container
        const viewsContainer = mainArea.createDiv();
        viewsContainer.style.flex = "1 1 auto";
        viewsContainer.style.display = "flex";
        viewsContainer.style.flexDirection = "column";
        viewsContainer.style.overflow = "hidden";

        this.viewChat = viewsContainer.createDiv();
        this.viewChat.style.display = "flex";
        this.viewChat.style.flexDirection = "column";
        this.viewChat.style.height = "100%";

        this.viewSheet = viewsContainer.createDiv();
        this.viewSheet.style.display = "none";
        this.viewSheet.style.flexDirection = "column";
        this.viewSheet.style.height = "100%";
        this.viewSheet.style.overflowY = "auto";
        this.viewSheet.style.padding = "15px";

        this.viewMaps = viewsContainer.createDiv();
        this.viewMaps.style.display = "none";
        this.viewMaps.style.flexDirection = "column";
        this.viewMaps.style.height = "100%";
        this.viewMaps.style.overflow = "hidden";
        this.viewMaps.style.padding = "15px";

        const switchTab = (activeBtn, activeView) => {
            [btnChat, btnSheet, btnMaps].forEach(b => b.style.borderBottom = "2px solid transparent");
            [this.viewChat, this.viewSheet, this.viewMaps].forEach(v => v.style.display = "none");
            activeBtn.style.borderBottom = "2px solid var(--interactive-accent)";
            activeView.style.display = "flex";
        };
        btnChat.addEventListener("click", () => switchTab(btnChat, this.viewChat));
        btnSheet.addEventListener("click", () => switchTab(btnSheet, this.viewSheet));
        btnMaps.addEventListener("click", () => switchTab(btnMaps, this.viewMaps));

        // Setup Dynamic Perspective Styles
        const styleEl = document.createElement('style');
        styleEl.id = 'dm-perspective-styles';
        document.head.appendChild(styleEl);
        this.clientCore.updatePerspectiveStyles();

        // Chat History Container
        this.ui.chatHistory = this.viewChat.createDiv({ cls: "dm-chat-history" });

        this.ui.chatHistory.style.flex = "1 1 auto";
        this.ui.chatHistory.style.overflowY = "auto";
        this.ui.chatHistory.style.padding = "10px";
        this.ui.chatHistory.style.border = "1px solid var(--background-modifier-border)";
        this.ui.chatHistory.style.marginBottom = "10px";

        // Enable Text Selection
        this.ui.chatHistory.style.userSelect = "text";
        this.ui.chatHistory.style.webkitUserSelect = "text";

        // Input Container
        const inputContainer = this.viewChat.createDiv({ cls: "dm-input-container" });

        // --- Dice Bar ---
        const diceBar = inputContainer.createDiv({ cls: "dm-dice-bar" });
        diceBar.style.display = "flex";
        diceBar.style.gap = "4px";
        diceBar.style.alignItems = "center";
        diceBar.style.flexWrap = "wrap";
        diceBar.style.padding = "4px 0";

        const DICE_TYPES = [
            { label: "d4", formula: "1d4" },
            { label: "d6", formula: "1d6" },
            { label: "d8", formula: "1d8" },
            { label: "d10", formula: "1d10" },
            { label: "d12", formula: "1d12" },
            { label: "2d6", formula: "2d6" },
        ];

        const makeDiceBtn = (label, formula, onClick) => {
            const btn = diceBar.createEl("button");
            btn.textContent = label;
            btn.style.padding = "3px 8px";
            btn.style.fontSize = "0.8em";
            btn.style.borderRadius = "3px";
            btn.style.border = "1px solid var(--background-modifier-border)";
            btn.style.background = "var(--background-modifier-form-field)";
            btn.style.color = "var(--text-normal)";
            btn.style.cursor = "pointer";
            btn.addEventListener("click", onClick);
        };

        DICE_TYPES.forEach(d => makeDiceBtn(d.label, d.formula, () => {
            this.clientCore.rollDice(d.formula, this.ui.diceReasonInput.value || d.label + " roll");
        }));

        // Modifier input
        const modInput = diceBar.createEl("input");
        modInput.type = "number";
        modInput.placeholder = "+MOD";
        modInput.style.width = "44px";
        modInput.style.padding = "3px 4px";
        modInput.style.fontSize = "0.8em";
        modInput.style.borderRadius = "3px";
        modInput.style.border = "1px solid var(--background-modifier-border)";
        modInput.style.background = "var(--background-modifier-form-field)";
        modInput.style.color = "var(--text-normal)";
        modInput.style.textAlign = "center";

        // d20 button — uses modifier if provided
        makeDiceBtn("d20", "1d20", () => {
            const modVal = modInput.value;
            const modStr = modVal ? (parseInt(modVal) >= 0 ? "+" + modVal : modVal) : "";
            this.clientCore.rollDice("1d20" + modStr, this.ui.diceReasonInput.value || "d20 roll");
        });

        // Reason input
        const reasonInput = diceBar.createEl("input");
        reasonInput.type = "text";
        reasonInput.placeholder = "Reason (e.g. Attack)";
        reasonInput.style.flex = "1";
        reasonInput.style.minWidth = "80px";
        reasonInput.style.padding = "3px 6px";
        reasonInput.style.fontSize = "0.8em";
        reasonInput.style.borderRadius = "3px";
        reasonInput.style.border = "1px solid var(--background-modifier-border)";
        reasonInput.style.background = "var(--background-modifier-form-field)";
        reasonInput.style.color = "var(--text-normal)";
        this.ui.diceReasonInput = reasonInput;


        inputContainer.style.flex = "0 0 auto";
        inputContainer.style.display = "flex";
        inputContainer.style.flexDirection = "column";
        inputContainer.style.gap = "5px";

        // Create the Multiline Textarea
        this.ui.chatInput = inputContainer.createEl("textarea");
        this.ui.chatInput.placeholder = `Playing as: ${this.clientCore.activeCharacter}
What do you do? (Shift+Enter for new line)`;
        this.ui.chatInput.style.width = "100%";
        this.ui.chatInput.style.resize = "none";
        this.ui.chatInput.style.minHeight = "40px";
        this.ui.chatInput.style.maxHeight = "160px"; // Roughly 8 lines
        this.ui.chatInput.style.overflowY = "auto";
        this.ui.chatInput.style.padding = "10px";
        this.ui.chatInput.style.fontFamily = "inherit";
        this.ui.chatInput.style.backgroundColor = "var(--background-modifier-form-field)";
        this.ui.chatInput.style.border = "1px solid var(--background-modifier-border)";
        this.ui.chatInput.style.color = "var(--text-normal)";
        this.ui.chatInput.style.borderRadius = "5px";

        // Auto-expand height as the user types
        this.ui.chatInput.addEventListener("input", () => {
            this.clientCore.handleTyping();
            this.ui.chatInput.style.height = "auto";
            this.ui.chatInput.style.height = Math.min(this.ui.chatInput.scrollHeight, 160) + "px";
        });

        // Send Button
        this.ui.sendBtn = inputContainer.createEl("button", { text: "Send" });
        this.ui.sendBtn.style.alignSelf = "flex-end";

        // Event Listeners (Both route to submitMessage)
        this.ui.sendBtn.addEventListener("click", () => this.clientCore.submitMessage());

        this.ui.chatInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault(); // Prevents adding a rogue newline on submit
                this.clientCore.submitMessage();
            }
        });

        this.clientCore.renderCharacterRadios();

        // Start the heartbeat synchronization loop
        this.clientCore.pollInterval = setInterval(() => this.clientCore.syncState(), 10000);
        this.clientCore.syncState();
    }
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = class DMEnginePlugin extends Plugin {
        async onload() {
            this.registerView(VIEW_TYPE_DM_CHAT, (leaf) => new DMChatView(leaf));
            this.addRibbonIcon('dice', 'Open DM Engine', () => this.activateView());
            this.addCommand({
                id: 'open-dm-chat',
                name: 'Open DM Chat',
                callback: () => this.activateView()
            });
        }

        async onunload() {
            this.app.workspace.detachLeavesOfType(VIEW_TYPE_DM_CHAT);
        }

        async activateView() {
            const { workspace } = this.app;
            let leaf = null;
            const leaves = workspace.getLeavesOfType(VIEW_TYPE_DM_CHAT);

            if (leaves.length > 0) {
                leaf = leaves[0];
            } else {
                leaf = workspace.getRightLeaf(false);
                if (leaf) await leaf.setViewState({ type: VIEW_TYPE_DM_CHAT, active: true });
            }
            if (leaf) workspace.revealLeaf(leaf);
        }
    }
}
