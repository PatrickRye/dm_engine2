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