const { requestUrl, ItemView, Plugin, Notice, MarkdownRenderer } = require('obsidian');

const VIEW_TYPE_DM_CHAT = "dm-chat-view";

class DMChatView extends ItemView {
    constructor(leaf) {
        super(leaf);
        this.activeCharacter = "Human DM"; // Default character name
        this.clientId = crypto.randomUUID(); // Unique ID for this connection
        this.listenController = null;
        this.pollInterval = null;
        this.serverUrl = window.localStorage.getItem("dm_server_url") || "http://127.0.0.1:8000";
    }

    getViewType() {
        return VIEW_TYPE_DM_CHAT;
    }

    getDisplayText() {
        return "DM Engine";
    }

async onOpen() {
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
        
        this.statusIndicator = titleContainer.createSpan({ text: "🟡 Checking..." });
        this.statusIndicator.style.fontSize = "0.8em";
        this.statusIndicator.style.fontWeight = "600";

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
                this.startListening();
            } else {
                if (this.listenController) {
                    this.listenController.abort();
                    this.listenController = null;
                }
            }
        });

        // Character Select Container
        this.charSelectContainer = container.createDiv();
        this.charSelectContainer.style.flex = "0 0 auto";
        this.charSelectContainer.style.padding = "5px 10px";
        this.charSelectContainer.style.display = "flex";
        this.charSelectContainer.style.flexWrap = "wrap";
        this.charSelectContainer.style.gap = "10px";
        this.charSelectContainer.style.borderBottom = "1px solid var(--background-modifier-border)";
        
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
        urlInput.value = this.serverUrl;
        urlInput.addEventListener("change", (e) => {
            this.serverUrl = e.target.value.trim().replace(/\/+$/, "");
            window.localStorage.setItem("dm_server_url", this.serverUrl);
            this.syncState();
        });

        this.rollAutomations = {
            hidden_rolls: true,
            saving_throws: true,
            skill_checks: true,
            attack_rolls: true
        };

        const createToggle = (key, labelText) => {
            const lbl = settingsContent.createEl("label");
            lbl.style.display = "flex";
            lbl.style.alignItems = "center";
            lbl.style.gap = "8px";
            lbl.style.cursor = "pointer";
            const cb = lbl.createEl("input", { type: "checkbox" });
            cb.checked = this.rollAutomations[key];
            cb.addEventListener("change", (e) => {
                this.rollAutomations[key] = e.target.checked;
                this.syncState();
            });
            lbl.appendChild(document.createTextNode(labelText));
        };

        createToggle("hidden_rolls", "Automate Hidden Rolls");
        createToggle("saving_throws", "Automate Saving Throws");
        createToggle("skill_checks", "Automate Skill Checks");
        createToggle("attack_rolls", "Automate Attack Rolls");
        
        // Setup Dynamic Perspective Styles
        const styleEl = document.createElement('style');
        styleEl.id = 'dm-perspective-styles';
        document.head.appendChild(styleEl);
        this.updatePerspectiveStyles();

        // Chat History Container
        this.chatContainer = container.createDiv({ cls: "dm-chat-history" });
        
        // 👇 2. Make the chat container "squishy" instead of a hardcoded height
        this.chatContainer.style.flex = "1 1 auto"; 
        this.chatContainer.style.overflowY = "auto";
        this.chatContainer.style.padding = "10px";
        this.chatContainer.style.border = "1px solid var(--background-modifier-border)";
        this.chatContainer.style.marginBottom = "10px";
        
        // Enable Text Selection
        this.chatContainer.style.userSelect = "text";
        this.chatContainer.style.webkitUserSelect = "text";

        // Input Container
        const inputContainer = container.createDiv({ cls: "dm-input-container" });
        
        // 👇 3. Anchor the input container to the bottom so it pushes UP when it grows
        inputContainer.style.flex = "0 0 auto"; 
        inputContainer.style.display = "flex";
        inputContainer.style.flexDirection = "column";
        inputContainer.style.gap = "5px";

        // Create the Multiline Textarea
        this.inputArea = inputContainer.createEl("textarea");
        this.inputArea.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do? (Shift+Enter for new line)`;
        this.inputArea.style.width = "100%";
        this.inputArea.style.resize = "none";
        this.inputArea.style.minHeight = "40px";
        this.inputArea.style.maxHeight = "160px"; // Roughly 8 lines
        this.inputArea.style.overflowY = "auto";
        this.inputArea.style.padding = "10px";
        this.inputArea.style.fontFamily = "inherit";
        this.inputArea.style.backgroundColor = "var(--background-modifier-form-field)";
        this.inputArea.style.border = "1px solid var(--background-modifier-border)";
        this.inputArea.style.color = "var(--text-normal)";
        this.inputArea.style.borderRadius = "5px";

        // Auto-expand height as the user types
        this.inputArea.addEventListener("input", () => {
            this.inputArea.style.height = "auto";
            this.inputArea.style.height = Math.min(this.inputArea.scrollHeight, 160) + "px";
        });

        // Send Button
        const sendBtn = inputContainer.createEl("button", { text: "Send" });
        sendBtn.style.alignSelf = "flex-end";

        // Event Listeners (Both route to submitMessage)
        sendBtn.addEventListener("click", () => this.submitMessage());
        
        this.inputArea.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault(); // Prevents adding a rogue newline on submit
                this.submitMessage();
            }
        });
        
        this.renderCharacterRadios();
        
        // Start the heartbeat synchronization loop
        this.pollInterval = setInterval(() => this.syncState(), 5000);
        this.syncState();
    }
    
    updatePerspectiveStyles() {
        const styleEl = document.getElementById('dm-perspective-styles');
        if (styleEl) {
            // Hides all perspectives by default, then explicitly un-hides "ALL" and the active character's name
            styleEl.textContent = `
                .perspective { display: none; margin-bottom: 10px; padding: 10px; border-left: 3px solid var(--interactive-accent); background: var(--background-modifier-hover); border-radius: 4px; }
                .perspective[data-target="ALL"] { display: block; border-left: none; background: transparent; padding: 0; }
                .perspective[data-target="${this.activeCharacter}"] { display: block; }
            `;
        }
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
            } else {
                this.setConnectionStatus(false);
            }
        } catch (e) {
            // Silently fail if server is down during heartbeat to prevent spamming errors
            this.setConnectionStatus(false);
        }
    }

    setConnectionStatus(isLive) {
        if (!this.statusIndicator) return;
        if (isLive) {
            this.statusIndicator.textContent = "🟢 Live";
            this.statusIndicator.style.color = "var(--text-success, #28a745)";
        } else {
            this.statusIndicator.textContent = "🔴 Disconnected";
            this.statusIndicator.style.color = "var(--text-error, #dc3545)";
        }
    }

    async fetchCharacterSheet() {
        try {
            const res = await fetch(`${this.serverUrl}/character_sheet`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.app.vault.adapter.getBasePath(), character: this.activeCharacter })
            });
            if (res.ok) {
                const data = await res.json();
                this.renderCharacterSheet(data);
            }
        } catch(e) {}
    }
    
    async fetchMaps() {
        if (this.isMapDragging) return; // Don't interrupt a drag with a background refresh
        try {
            const res = await fetch(`${this.serverUrl}/map_state`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.app.vault.adapter.getBasePath() })
            });
            if (res.ok) {
                const data = await res.json();
                if (!this.isMapDragging) this.renderMaps(data);
            }
        } catch(e) {}
    }

    renderCharacterSheet(data) {
        if (!data || data.error) {
            this.viewSheet.innerHTML = `<div style="color:var(--text-error);">${data ? data.error : "Failed to load sheet."}</div>`;
            return;
        }
        const s = data.sheet;
        const hp = s.hp !== undefined ? s.hp : "?";
        const maxHp = s.max_hp !== undefined ? s.max_hp : "?";
        const conds = s.active_conditions ? s.active_conditions.map(c => c.name).join(", ") : "None";
        const equip = s.equipment ? Object.entries(s.equipment).map(([k,v]) => `<li><b>${k.replace('_',' ')}</b>: ${v}</li>`).join("") : "None";
        const res = s.resources ? Object.entries(s.resources).map(([k,v]) => `<li><b>${k}</b>: ${v}</li>`).join("") : "None";
        
        this.viewSheet.innerHTML = `
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
        this.viewMaps.empty();
        if (!data || !data.map_data || (!data.map_data.walls.length && !data.map_data.dm_map_image_path)) {
            this.viewMaps.createEl("p", { text: "No active maps loaded in engine.", style: "color:var(--text-muted);" });
            return;
        }
        
        const mapData = data.map_data;
        const entities = data.entities || [];

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
        this.viewMaps.appendChild(canvas);

        const ctx = canvas.getContext('2d');
        const SCALE = 15; // 15 pixels per foot. A 5ft square = 75px.

        let bgImageRef = null;
        const drawScene = (bgImg) => {
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
                    if (this.loadedImages[ent.icon_url]) {
                        ctx.save();
                        ctx.clip(); // Mask the image inside the circle
                        ctx.drawImage(this.loadedImages[ent.icon_url], px - pRadius, py - pRadius, pRadius * 2, pRadius * 2);
                        ctx.restore();
                    } else {
                        const img = new Image();
                        img.onload = () => { this.loadedImages[ent.icon_url] = img; drawScene(bgImageRef); };
                        img.src = `${this.serverUrl}/vault_media?filepath=${encodeURIComponent(ent.icon_url)}`;
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
        };

        if (imagePath) {
            if (this.loadedImages[imagePath]) {
                drawScene(this.loadedImages[imagePath]);
            } else {
                const img = new Image();
                img.onload = () => {
                    this.loadedImages[imagePath] = img;
                    drawScene(img);
                };
                img.src = `${this.serverUrl}/vault_media?filepath=${encodeURIComponent(imagePath)}`;
            }
        } else {
            drawScene(null);
        }

        // --- DRAG AND DROP LOGIC ---
        canvas.addEventListener('mousedown', (e) => {
            if (this.activeCharacter !== "Human DM") return;
            const rect = canvas.getBoundingClientRect();
            const mouseX = (e.clientX - rect.left) * (canvas.width / rect.width);
            const mouseY = (e.clientY - rect.top) * (canvas.height / rect.height);

            // Check collision backwards (top-most drawn entity selected first)
            for (let i = entities.length - 1; i >= 0; i--) {
                const ent = entities[i];
                if (ent.hp <= 0) continue;
                if (Math.hypot(mouseX - (ent.x * SCALE), mouseY - (ent.y * SCALE)) <= (ent.size / 2) * SCALE) {
                    this.isMapDragging = true;
                    canvas.draggedEntity = ent;
                    break;
                }
            }
        });

        canvas.addEventListener('mousemove', (e) => {
            if (this.isMapDragging && canvas.draggedEntity) {
                const rect = canvas.getBoundingClientRect();
                canvas.draggedEntity.x = ((e.clientX - rect.left) * (canvas.width / rect.width)) / SCALE;
                canvas.draggedEntity.y = ((e.clientY - rect.top) * (canvas.height / rect.height)) / SCALE;
                drawScene(bgImageRef); // Live re-render
            }
        });

        const stopDrag = async () => {
            if (this.isMapDragging && canvas.draggedEntity) {
                const ent = canvas.draggedEntity;
                this.isMapDragging = false;
                canvas.draggedEntity = null;
                try {
                    await fetch(`${this.serverUrl}/ooc_move_entity`, {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ vault_path: this.app.vault.adapter.getBasePath(), entity_name: ent.name, x: ent.x, y: ent.y })
                    });
                } catch(err) { console.error("Failed to move entity", err); }
            }
        };

        canvas.addEventListener('mouseup', stopDrag);
        canvas.addEventListener('mouseout', stopDrag);
    }

    updateRadioUI(lockedCharacters) {
        const chars = new Set(["Human DM"]);
        const files = this.app.vault.getMarkdownFiles();
        for (const file of files) {
            const cache = this.app.metadataCache.getFileCache(file);
            if (cache && cache.frontmatter && cache.frontmatter.tags) {
                const tags = Array.isArray(cache.frontmatter.tags) ? cache.frontmatter.tags : [cache.frontmatter.tags];
                if (tags.some(t => String(t).toLowerCase() === 'pc' || String(t).toLowerCase() === 'player')) {
                    chars.add(cache.frontmatter.name || file.basename);
                }
            }
        }

        const existingRadios = Array.from(this.charSelectContainer.querySelectorAll('input[type="radio"]'));
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
            // Gracefully update disabled states without re-rendering DOM elements
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
        this.charSelectContainer.empty();
        
        let chars = precalculatedChars;
        if (!chars) {
            chars = new Set(["Human DM"]);
            const files = this.app.vault.getMarkdownFiles();
            for (const file of files) {
                const cache = this.app.metadataCache.getFileCache(file);
                if (cache && cache.frontmatter && cache.frontmatter.tags) {
                    const tags = Array.isArray(cache.frontmatter.tags) ? cache.frontmatter.tags : [cache.frontmatter.tags];
                    if (tags.some(t => String(t).toLowerCase() === 'pc' || String(t).toLowerCase() === 'player')) {
                        chars.add(cache.frontmatter.name || file.basename);
                    }
                }
            }
        }
        
        if (!chars.has(this.activeCharacter)) {
            this.activeCharacter = "Human DM";
        }

        for (const char of chars) {
            const lbl = this.charSelectContainer.createEl("label");
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
                        this.inputArea.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do? (Shift+Enter for new line)`;
                        this.appendMessage("System", `Active character switched to: **${this.activeCharacter}**`, "var(--text-faint)");
                        
                        // Instantly fire a heartbeat to secure the lock for everyone else
                        this.syncState();
                    } catch (err) {
                        this.appendMessage("System", `**Error swapping character:** ${err.message}`, "red");
                        // Revert visual selection
                        const radios = Array.from(this.charSelectContainer.querySelectorAll('input[type="radio"]'));
                        const oldRadio = radios.find(r => r.value === this.activeCharacter);
                        if (oldRadio) oldRadio.checked = true;
                    }
                }
            });
        }
        
        if(this.inputArea) this.inputArea.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do? (Shift+Enter for new line)`;
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
                            if (data.status === "streaming" || data.status === "error") {
                                if (!msgDiv) {
                                    msgDiv = this.chatContainer.createDiv({ cls: "dm-message" });
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
                    await MarkdownRenderer.renderMarkdown(accumulatedText, contentDiv, "", this);
                    const paragraphs = contentDiv.querySelectorAll("p");
                    paragraphs.forEach(p => { p.style.marginTop = "0"; p.style.marginBottom = "0.5em"; });
                    this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
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
        const text = this.inputArea.value.trim();
        if (!text) return;

        // Enforce OOC rules
        if (text.startsWith(">") && this.activeCharacter !== "Human DM") {
            new Notice("Only the 'Human DM' is allowed to execute OOC commands (>).");
            this.appendMessage("System", "Only the 'Human DM' is allowed to execute OOC commands (>).", "red");
            this.inputArea.value = "";
            return;
        }

        // 2. Clear input, reset height, and disable while thinking
        this.inputArea.value = "";
        this.inputArea.style.height = "auto";
        this.inputArea.style.height = "40px";
        this.inputArea.disabled = true;

        // 3. Post user message to UI
        this.appendMessage(this.activeCharacter, text, "var(--text-accent)");

        // 4. Add "Thinking..." Indicator
        const loadingDiv = this.chatContainer.createDiv({ cls: "dm-loading" });
        loadingDiv.style.fontStyle = "italic";
        loadingDiv.style.color = "var(--text-muted)";
        loadingDiv.style.marginTop = "10px";
        loadingDiv.innerHTML = "🎲 DM is thinking...";
        this.chatContainer.scrollTop = this.chatContainer.scrollHeight;

        // 5. Send request using native fetch to support ReadableStream (SSE)
        try {
            const response = await fetch(`${this.serverUrl}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: text, 
                    character: this.activeCharacter, 
                    vault_path: this.app.vault.adapter.getBasePath(),
                    client_id: this.clientId,
                    roll_automations: this.rollAutomations
                })
            });

            // Remove loading indicator early
            loadingDiv.remove();
            
            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`HTTP error! status: ${response.status} - ${errorText}`);
            }
            
            // 6. Setup streaming message container
            const msgDiv = this.chatContainer.createDiv({ cls: "dm-message" });
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
                    await MarkdownRenderer.renderMarkdown(accumulatedText, contentDiv, "", this);
                    
                    // Remove the default paragraph margins provided by MarkdownRenderer for better chat spacing
                    const paragraphs = contentDiv.querySelectorAll("p");
                    paragraphs.forEach(p => {
                        p.style.marginTop = "0";
                        p.style.marginBottom = "0.5em";
                    });
                    
                    this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
                }
            }

        } catch (error) {
            loadingDiv.remove();
            console.error("Network/Stream Error:", error);
            new Notice("Failed to reach DM Engine or stream was interrupted.");
            this.appendMessage("System", `**Error:** ${error.message}`, "red");
        } finally {
            // Re-enable the input box
            this.inputArea.disabled = false;
            this.inputArea.focus();
        }
    }

    async appendMessage(sender, text, color) {
        const msgDiv = this.chatContainer.createDiv({ cls: "dm-message" });
        msgDiv.style.marginBottom = "15px";
        msgDiv.style.lineHeight = "1.5";
        
        const senderSpan = msgDiv.createSpan({ text: `${sender}: ` });
        senderSpan.style.fontWeight = "bold";
        senderSpan.style.color = color;

        const contentDiv = msgDiv.createDiv({ cls: "dm-message-content" });
        contentDiv.style.marginTop = "5px";
        
        await MarkdownRenderer.renderMarkdown(text, contentDiv, "", this);

        const paragraphs = contentDiv.querySelectorAll("p");
        paragraphs.forEach(p => {
            p.style.marginTop = "0";
            p.style.marginBottom = "0.5em";
        });

        this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
    }
}

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