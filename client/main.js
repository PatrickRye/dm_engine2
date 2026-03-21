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

class DMEngineClientCore {
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
    }

    animatePings() {
        if (!this.activePings) return;
        const now = Date.now();
        this.activePings = this.activePings.filter(p => now - p.time < 3000);

        if (this.drawSceneRef) {
            this.drawSceneRef();
        }

        if (this.activePings.length > 0) {
            this.pingAnimationId = requestAnimationFrame(() => this.animatePings());
        } else {
            this.pingAnimationId = null;
        }
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
                body: JSON.stringify({ client_id: this.clientId, character: this.activeCharacter, roll_automations: this.rollAutomations })
            });

            if (response.ok) {
                const data = await response.json();
                this.updateRadioUI(data.locked_characters || []);
                this.setConnectionStatus(true);

                this.fetchCharacterSheet();
                this.fetchMaps();

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
        const conds = s.active_conditions ? s.active_conditions.map(c => c.name).join(", ") : "None";
        const equip = s.equipment ? Object.entries(s.equipment).map(([k, v]) => `<li><b>${k.replace('_', ' ')}</b>: ${v}</li>`).join("") : "None";
        const res = s.resources ? Object.entries(s.resources).map(([k, v]) => `<li><b>${k}</b>: ${v}</li>`).join("") : "None";

        if (this.view.viewSheet) this.view.viewSheet.innerHTML = `
            <h2 style="margin-top:0;">${s.name}</h2>
            <div style="display:flex; gap:10px; margin-bottom:15px;">
                <div style="background:var(--background-modifier-form-field); padding:10px; border-radius:5px; flex:1; text-align:center;"><b>HP</b><br><span style="font-size:1.5em; color:var(--text-success);">${hp} / ${maxHp}</span></div>
                <div style="background:var(--background-modifier-form-field); padding:10px; border-radius:5px; flex:1; text-align:center;"><b>AC</b><br><span style="font-size:1.5em;">${s.ac || 10}</span></div>
            </div>
            <p><b>Conditions:</b> <span style="color:var(--text-error);">${conds}</span></p>
            <p><b>Spell Slots:</b> ${s.spell_slots || "N/A"}</p>
            <p><b>Attunement:</b> ${s.attunement_slots || "0/3"}</p>
            <h4 style="margin-bottom:5px;">Resources</h4><ul style="margin-top:0;">${res}</ul>
            <h4 style="margin-bottom:5px;">Equipment</h4><ul style="margin-top:0;">${equip}</ul>
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

            const canvas = document.createElement('canvas');
            canvas.width = 1600; // Arbitrary bounds, can be scrolled within the tab
            canvas.height = 1600;
            canvas.style.backgroundColor = "var(--background-modifier-form-field)";
            canvas.style.borderRadius = "4px";
            canvasContainer.appendChild(canvas);

            const ctx = canvas.getContext('2d');
            const SCALE = 15; // 15 pixels per foot. A 5ft square = 75px.

            let bgImageRef = null;

            aoeShapeSelect.addEventListener("change", (e) => {
                this.aoeMode = e.target.value === "none" ? null : e.target.value;
                if (typeof drawScene === 'function') drawScene(bgImageRef);
            });

            sizeInput.addEventListener("change", (e) => {
                this.aoeSize = parseInt(e.target.value) || 20;
                if (typeof drawScene === 'function') drawScene(bgImageRef);
            });

            const drawScene = (bgImg) => {
                this.drawSceneRef = drawScene;
                bgImageRef = bgImg || bgImageRef;
                ctx.clearRect(0, 0, canvas.width, canvas.height);

                if (bgImageRef) ctx.drawImage(bgImageRef, 0, 0);

                // Draw Grid
                ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
                ctx.lineWidth = 1;
                for (let i = 0; i < canvas.width; i += SCALE * mapData.grid_scale) {
                    ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, canvas.height); ctx.stroke();
                    ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(canvas.width, i); ctx.stroke();
                }

                // Draw Fog of War Mask
                if (this.activeCharacter !== "Human DM") {
                    ctx.fillStyle = "rgba(0, 0, 0, 0.98)"; // Players see solid black
                } else {
                    ctx.fillStyle = "rgba(0, 0, 50, 0.4)"; // DM sees a faint blue tint for unexplored areas
                }
                ctx.fillRect(0, 0, canvas.width, canvas.height);

                ctx.globalCompositeOperation = 'destination-out';
                (mapData.explored_areas || []).forEach(area => {
                    const [x, y, radius] = area;
                    ctx.beginPath();
                    ctx.arc(x * SCALE, y * SCALE, radius * SCALE, 0, Math.PI * 2);
                    ctx.fill(); // Punch a transparent hole through the Fog of War!
                });
                ctx.globalCompositeOperation = 'source-over';

                // Draw Walls
                const activeWalls = [...(mapData.walls || []), ...(mapData.temporary_walls || [])];
                activeWalls.forEach(wall => {
                    if (!this.is_visible_to_player(wall.start[0], wall.start[1]) && !this.is_visible_to_player(wall.end[0], wall.end[1])) {
                        return;
                    }
                    ctx.beginPath();
                    ctx.moveTo(wall.start[0] * SCALE, wall.start[1] * SCALE);
                    ctx.lineTo(wall.end[0] * SCALE, wall.end[1] * SCALE);

                    if (!wall.is_solid && wall.is_visible) {
                        ctx.strokeStyle = "rgba(40, 167, 69, 0.6)"; // Open door (Green)
                        ctx.lineWidth = 4;
                    } else if (!wall.is_visible) {
                        ctx.strokeStyle = "rgba(0, 150, 255, 0.4)"; // Window/Glass (Blue)
                        ctx.lineWidth = 2;
                    } else {
                        ctx.strokeStyle = "rgba(220, 53, 69, 0.8)"; // Solid wall (Red)
                        ctx.lineWidth = 3;
                    }
                    ctx.stroke();
                });

                // Draw Entities
                entities.forEach(ent => {
                    if (ent.hp <= 0) return;

                    const px = ent.x * SCALE;
                    const py = ent.y * SCALE;
                    const pRadius = (ent.size / 2) * SCALE;

                    // Enforce FoW visibility for players looking at NPCs
                    if (this.activeCharacter !== "Human DM" && !ent.is_pc) {
                        let isRevealed = false;
                        for (const area of mapData.explored_areas || []) {
                            if (Math.hypot(ent.x - area[0], ent.y - area[1]) <= area[2]) { isRevealed = true; break; }
                        }
                        if (!isRevealed) return; // Do not draw hidden monsters!
                    }

                    ctx.beginPath();
                    ctx.arc(px, py, pRadius, 0, Math.PI * 2);

                    if (ent.icon_url) {
                        if (this.loadedImages[ent.icon_url] instanceof Image) {
                            ctx.save();
                            ctx.clip(); // Mask the image inside the circle
                            ctx.drawImage(this.loadedImages[ent.icon_url], px - pRadius, py - pRadius, pRadius * 2, pRadius * 2);
                            ctx.restore();
                        } else {
                            if (this.loadedImages[ent.icon_url] === undefined) {
                                this.loadedImages[ent.icon_url] = "loading";
                                const img = new Image();
                                img.onload = () => { this.loadedImages[ent.icon_url] = img; drawScene(bgImageRef); };
                                img.onerror = () => { this.loadedImages[ent.icon_url] = "failed"; drawScene(bgImageRef); };
                                img.src = `${this.serverUrl}/vault_media?filepath=${encodeURIComponent(ent.icon_url)}`;
                            }
                            ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545"; ctx.fill(); // Fallback color while loading
                        }
                    } else {
                        ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545"; // Blue for PCs, Red for Monsters
                        ctx.fill();
                    }

                    ctx.strokeStyle = "#ffffff";
                    ctx.lineWidth = 2;
                    ctx.stroke();

                    ctx.fillStyle = "white";
                    ctx.font = "bold 12px sans-serif";
                    ctx.textAlign = "center";
                    ctx.fillText(ent.name, px, py - pRadius - 5);
                });

                knownTraps.forEach((trap) => {
                    if (this.is_visible_to_player(trap.x, trap.y)) {
                        const px = trap.x * SCALE;
                        const py = trap.y * SCALE;
                        ctx.fillStyle = "red";
                        ctx.font = "bold 20px sans-serif";
                        ctx.textAlign = "center";
                        ctx.fillText("X", px, py);
                    }
                });

                // Draw server-synchronized proposed paths
                activePaths.forEach(p => {
                    if (this.activeCharacter !== "Human DM" && p.entity_name !== this.activeCharacter) return;

                    // Draw proposed path (orange if valid but pending confirm, red if invalid)
                    if (p.waypoints && p.waypoints.length > 1) {
                        ctx.beginPath();
                        ctx.moveTo(p.waypoints[0][0] * SCALE, p.waypoints[0][1] * SCALE);
                        for (let i = 1; i < p.waypoints.length; i++) {
                            ctx.lineTo(p.waypoints[i][0] * SCALE, p.waypoints[i][1] * SCALE);
                        }
                        ctx.strokeStyle = p.is_valid ? "rgba(255, 165, 0, 0.8)" : "rgba(220, 53, 69, 0.8)";
                        ctx.lineWidth = 3;
                        ctx.setLineDash([5, 5]);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }

                    // Draw alternative path (solid yellow) if invalid
                    if (!p.is_valid && p.alternative_path && p.alternative_path.length > 1) {
                        ctx.beginPath();
                        ctx.moveTo(p.alternative_path[0][0] * SCALE, p.alternative_path[0][1] * SCALE);
                        for (let i = 1; i < p.alternative_path.length; i++) {
                            ctx.lineTo(p.alternative_path[i][0] * SCALE, p.alternative_path[i][1] * SCALE);
                        }
                        ctx.strokeStyle = "rgba(255, 255, 0, 1.0)";
                        ctx.lineWidth = 4;
                        ctx.stroke();
                    }
                });

                // Draw Drag Path Line
                if (this.isMapDragging && canvas.draggedEntity && canvas.dragStartX !== undefined && canvas.dragStartY !== undefined) {
                    const startX = canvas.dragStartX * SCALE;
                    const startY = canvas.dragStartY * SCALE;
                    const currentX = canvas.draggedEntity.x * SCALE;
                    const currentY = canvas.draggedEntity.y * SCALE;

                    ctx.beginPath();
                    ctx.moveTo(startX, startY);
                    ctx.lineTo(currentX, currentY);
                    ctx.strokeStyle = this.activeCharacter === "Human DM" ? "rgba(255, 200, 0, 0.8)" : "rgba(40, 167, 69, 0.8)";
                    ctx.lineWidth = 4;
                    ctx.setLineDash([8, 6]);
                    ctx.stroke();
                    ctx.setLineDash([]); // Reset line dash for next renders

                    // Calculate Chebyshev distance (D&D 5e standard grid distance)
                    const distFt = Math.max(Math.abs(canvas.draggedEntity.x - canvas.dragStartX), Math.abs(canvas.draggedEntity.y - canvas.dragStartY));

                    ctx.fillStyle = "white";
                    ctx.font = "bold 14px sans-serif";
                    ctx.textAlign = "center";
                    ctx.fillText(`${Math.round(distFt)} ft`, (startX + currentX) / 2, (startY + currentY) / 2 - 10);
                }

                // --- DRAW AOE PREVIEW ---
                if (this.aoeMode) {
                    ctx.fillStyle = "rgba(255, 100, 0, 0.3)";
                    ctx.strokeStyle = "rgba(255, 100, 0, 0.8)";
                    ctx.lineWidth = 2;
                    const sizePx = this.aoeSize * SCALE;
                    const mx = this.mouseX * SCALE;
                    const my = this.mouseY * SCALE;

                    const activeEnt = entities.find(e => e.name === this.activeCharacter);

                    if (activeEnt) {
                        const ex = activeEnt.x * SCALE;
                        const ey = activeEnt.y * SCALE;

                        ctx.beginPath();
                        ctx.moveTo(ex, ey);
                        ctx.lineTo(mx, my);
                        ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
                        ctx.lineWidth = 2;
                        ctx.setLineDash([4, 4]);
                        ctx.stroke();
                        ctx.setLineDash([]);

                        const distFt = Math.max(Math.abs(this.mouseX - activeEnt.x), Math.abs(this.mouseY - activeEnt.y));
                        const text = `${Math.round(distFt)} ft`;
                        const midX = (ex + mx) / 2;
                        const midY = (ey + my) / 2 - 10;

                        ctx.font = "bold 14px sans-serif";
                        ctx.textAlign = "center";
                        ctx.lineWidth = 3;
                        ctx.strokeStyle = "black";
                        ctx.strokeText(text, midX, midY);
                        ctx.fillStyle = "white";
                        ctx.fillText(text, midX, midY);

                        // Reset fill/stroke for the AoE shape
                        ctx.fillStyle = "rgba(255, 100, 0, 0.3)";
                        ctx.strokeStyle = "rgba(255, 100, 0, 0.8)";
                        ctx.lineWidth = 2;
                    }

                    if (this.aoeMode === "circle") {
                        ctx.beginPath();
                        ctx.arc(mx, my, sizePx, 0, Math.PI * 2);
                        ctx.fill(); ctx.stroke();
                    } else if (this.aoeMode === "cube") {
                        ctx.fillRect(mx - sizePx / 2, my - sizePx / 2, sizePx, sizePx);
                        ctx.strokeRect(mx - sizePx / 2, my - sizePx / 2, sizePx, sizePx);
                    } else if (this.aoeMode === "cone" || this.aoeMode === "line") {
                        if (activeEnt) {
                            const ex = activeEnt.x * SCALE;
                            const ey = activeEnt.y * SCALE;
                            const angle = Math.atan2(my - ey, mx - ex);

                            ctx.beginPath();
                            ctx.moveTo(ex, ey);
                            if (this.aoeMode === "cone") {
                                ctx.arc(ex, ey, sizePx, angle - Math.PI / 6, angle + Math.PI / 6);
                            } else {
                                const halfWidth = (5 / 2) * SCALE;
                                const p1x = ex - halfWidth * Math.sin(angle);
                                const p1y = ey + halfWidth * Math.cos(angle);
                                const p2x = ex + halfWidth * Math.sin(angle);
                                const p2y = ey - halfWidth * Math.cos(angle);
                                const p3x = p2x + sizePx * Math.cos(angle);
                                const p3y = p2y + sizePx * Math.sin(angle);
                                const p4x = p1x + sizePx * Math.cos(angle);
                                const p4y = p1y + sizePx * Math.sin(angle);
                                ctx.moveTo(p1x, p1y);
                                ctx.lineTo(p2x, p2y);
                                ctx.lineTo(p3x, p3y);
                                ctx.lineTo(p4x, p4y);
                            }
                            ctx.closePath();
                            ctx.fill(); ctx.stroke();
                        } else {
                            ctx.fillStyle = "white";
                            ctx.font = "bold 14px sans-serif";
                            ctx.fillText("Select your character to project lines/cones", mx, my - 10);
                        }
                    }
                }

                // --- DRAW PINGS ---
                if (this.activePings) {
                    const now = Date.now();
                    this.activePings.forEach(p => {
                        const age = now - p.time;
                        if (age > 3000) return;

                        const maxRadius = 45;
                        const progress = age / 1000;
                        const pulse = progress % 1;
                        const radius = pulse * maxRadius;
                        const alpha = 1 - pulse;

                        const px = p.x * SCALE;
                        const py = p.y * SCALE;

                        ctx.beginPath(); ctx.arc(px, py, Math.max(0.1, radius), 0, Math.PI * 2);
                        ctx.strokeStyle = `rgba(220, 53, 69, ${alpha})`; ctx.lineWidth = 3; ctx.stroke();
                        ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2);
                        ctx.fillStyle = `rgba(220, 53, 69, ${Math.max(0, 1 - age / 3000)})`; ctx.fill();
                        ctx.fillStyle = `rgba(255, 255, 255, ${Math.max(0, 1 - age / 3000)})`;
                        ctx.font = "bold 14px sans-serif"; ctx.textAlign = "center";
                        ctx.fillText(p.character, px, py - 20);
                    });
                }
            };

            if (imagePath) {
                if (this.loadedImages[imagePath] instanceof Image) {
                    drawScene(this.loadedImages[imagePath]);
                } else if (this.loadedImages[imagePath] === "failed") {
                    drawScene(null);
                } else if (this.loadedImages[imagePath] === undefined) {
                    this.loadedImages[imagePath] = "loading";
                    const img = new Image();
                    img.onload = () => {
                        this.loadedImages[imagePath] = img;
                        drawScene(img);
                    };
                    img.onerror = () => {
                        console.error("Failed to load map image:", imagePath);
                        this.loadedImages[imagePath] = "failed";
                        drawScene(null);
                    };
                    img.src = `${this.serverUrl}/vault_media?filepath=${encodeURIComponent(imagePath)}`;
                } else {
                    drawScene(null); // Waiting for load
                }
            } else {
                drawScene(null);
            }

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
                    drawScene(bgImageRef);
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
                    drawScene(bgImageRef);
                } else if (this.isMapDragging && canvas.draggedEntity) {
                    canvas.draggedEntity.x = newX;
                    canvas.draggedEntity.y = newY;
                    drawScene(bgImageRef); // Live re-render
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
                            drawScene(bgImageRef);
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
        if (this.listenController) {
            this.listenController.abort();
        }
        this.listenController = new AbortController();
        try {
            const response = await fetch(`${this.serverUrl}/listen?client_id=${this.clientId}`, {
                method: "GET",
                signal: this.listenController.signal
            });

            if (!response.ok) return;

            const reader = response.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let buffer = "";
            let msgDiv = null;
            let contentDiv = null;
            let accumulatedText = "";

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

                            if (data.type === "ping") {
                                this.activePings = this.activePings || [];
                                this.activePings.push({ x: data.x, y: data.y, character: data.character, time: Date.now() });
                                if (!this.pingAnimationId) this.animatePings();
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
                }
            }
        } catch (e) {
            if (e.name !== "AbortError") {
                console.error("Listen Error:", e);
                new Notice("Listen stream disconnected.");
            }
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

        this.appendMessage(this.activeCharacter, text, "var(--text-accent)");

        const loadingDiv = this.view.ui.chatHistory.createDiv({ cls: "dm-loading" });
        loadingDiv.style.fontStyle = "italic";
        loadingDiv.style.color = "var(--text-muted)";
        loadingDiv.style.marginTop = "10px";
        loadingDiv.innerHTML = "🎲 DM is thinking...";
        this.view.ui.chatHistory.scrollTop = this.view.ui.chatHistory.scrollHeight;

        try {
            const response = await fetch(`${this.serverUrl}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: text,
                    character: this.activeCharacter,
                    vault_path: this.vaultPath,
                    client_id: this.clientId,
                    roll_automations: this.rollAutomations
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

        // Top Control Bar
        const topBar = container.createDiv();
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
        this.ui.charSelect = container.createDiv();
        this.ui.charSelect.style.flex = "0 0 auto";
        this.ui.charSelect.style.padding = "5px 10px";
        this.ui.charSelect.style.display = "flex";
        this.ui.charSelect.style.flexWrap = "wrap";
        this.ui.charSelect.style.gap = "10px";
        this.ui.charSelect.style.borderBottom = "1px solid var(--background-modifier-border)";

        // Collapsible Settings Panel
        const settingsWrapper = container.createDiv({ cls: "dm-settings-wrapper" });
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
        const tabBar = container.createDiv({ cls: "dm-tab-bar" });
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
        const viewsContainer = container.createDiv();
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
        this.clientCore.pollInterval = setInterval(() => this.clientCore.syncState(), 5000);
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
