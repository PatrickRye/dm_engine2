class DMEngineClientCore {
    constructor(ui, platform) {
        this.ui = ui;
        this.platform = platform; // "web" or "obsidian"
        
        // State
        this.activeCharacter = "Human DM";
        this.clientId = crypto.randomUUID();
        this.vaultPath = "";
        this.serverUrl = "http://127.0.0.1:8000";
        this.listenController = null;
        this.pollInterval = null;
        this.availableCharacters = new Set(["Human DM"]);
        this.loadedImages = {};
        this.isMapDragging = false;
        this.isDrawingPath = false;
        this.waypoints = [];
        this.rollAutomations = {
            hidden_rolls: true,
            saving_throws: true,
            skill_checks: true,
            attack_rolls: true,
        };

        if (this.platform === "web") {
            this.vaultPath = localStorage.getItem("dm_vault_path") || "";
            this.serverUrl = localStorage.getItem("dm_server_url_web") || "http://127.0.0.1:8000";
        }
    }

    updatePerspectiveStyles() {
        let styleEl = document.getElementById("dm-perspective-styles");
        if (!styleEl) {
            styleEl = document.createElement("style");
            styleEl.id = "dm-perspective-styles";
            document.head.appendChild(styleEl);
        }
        styleEl.textContent = `
            .perspective { display: none; margin-bottom: 10px; padding: 10px; border-left: 3px solid #7289da; background: rgba(114, 137, 218, 0.1); border-radius: 4px; }
            .perspective[data-target="ALL"] { display: block; border-left: none; background: transparent; padding: 0; }
            .perspective[data-target="${this.activeCharacter}"] { display: block; }
        `;
    }

    async syncState() {
        if (!this.vaultPath) return;
        try {
            const response = await fetch(`${this.serverUrl}/heartbeat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    client_id: this.clientId,
                    character: this.activeCharacter,
                    roll_automations: this.rollAutomations,
                }),
            });

            if (response.ok) {
                const data = await response.json();
                this.renderCharacterRadios(data.locked_characters || []);
                this.setConnectionStatus(true);

                this.fetchCharacterSheet();
                this.fetchMaps();
            } else {
                this.setConnectionStatus(false);
            }
        } catch (e) {
            this.setConnectionStatus(false);
        }
    }

    setConnectionStatus(isLive) {
        if (isLive) {
            this.ui.status.textContent = "🟢 Live";
            this.ui.status.style.color = "var(--text-success)";
        } else {
            this.ui.status.textContent = "🔴 Disconnected";
            this.ui.status.style.color = "var(--text-error)";
        }
    }

    async fetchCharacterSheet() {
        try {
            const res = await fetch(`${this.serverUrl}/character_sheet`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    vault_path: this.vaultPath,
                    character: this.activeCharacter,
                }),
            });
            if (res.ok) {
                const data = await res.json();
                this.renderCharacterSheet(data);
            }
        } catch (e) {}
    }

    async fetchMaps() {
        if (this.isMapDragging) return;
        try {
            const res = await fetch(`${this.serverUrl}/map_state`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath }),
            });
            if (res.ok) {
                const data = await res.json();
                if (!this.isMapDragging) this.renderMaps(data);
            }
        } catch (e) {}
    }

    renderCharacterSheet(data) {
      if (!data || data.error) {
        this.ui.viewSheet.innerHTML = `<div style="color:var(--text-error);">${data ? data.error : "Failed to load sheet."}</div>`;
        return;
      }
      const s = data.sheet;
      const hp = s.hp !== undefined ? s.hp : "?";
      const maxHp = s.max_hp !== undefined ? s.max_hp : "?";
      const conds = s.active_conditions
        ? s.active_conditions.map((c) => c.name).join(", ")
        : "None";
      const equip = s.equipment
        ? Object.entries(s.equipment)
            .map(([k, v]) => `<li><b>${k.replace("_", " ")}</b>: ${v}</li>`)
            .join("")
        : "None";
      const res = s.resources
        ? Object.entries(s.resources)
            .map(([k, v]) => `<li><b>${k}</b>: ${v}</li>`)
            .join("")
        : "None";

      this.ui.viewSheet.innerHTML = `
            <h2 style="margin-top:0;">${s.name}</h2>
            <div style="display:flex; gap:10px; margin-bottom:15px;">
                <div class="stat-box"><b>HP</b><span style="color:var(--text-success); display:block; margin-top:5px;">${hp} / ${maxHp}</span></div>
                <div class="stat-box"><b>AC</b><span style="display:block; margin-top:5px;">${s.ac || 10}</span></div>
            </div>
            <p><b>Conditions:</b> <span style="color:var(--text-error);">${conds}</span></p>
            <p><b>Spell Slots:</b> ${s.spell_slots || "N/A"}</p>
            <p><b>Attunement:</b> ${s.attunement_slots || "0/3"}</p>
            <h4 style="margin-bottom:5px;">Resources</h4><ul style="margin-top:0;">${res}</ul>
            <h4 style="margin-bottom:5px;">Equipment</h4><ul style="margin-top:0;">${equip}</ul>
        `;
    }

    renderMaps(data) {
      this.ui.viewMaps.innerHTML = "";
      if (
        !data ||
        !data.map_data ||
        (!data.map_data.walls.length && !data.map_data.dm_map_image_path)
      ) {
        this.ui.viewMaps.innerHTML =
          "<p style='color:var(--text-muted);'>No active maps loaded in engine.</p>";
        return;
      }

      const mapData = data.map_data;
      const entities = data.entities || [];
      const knownTraps = data.known_traps || [];

      let imagePath = null;
      if (this.activeCharacter === "Human DM") {
        imagePath = mapData.dm_map_image_path || mapData.player_map_image_path;
      } else {
        imagePath = mapData.player_map_image_path || mapData.dm_map_image_path;
      }

      const canvas = document.createElement("canvas");
      canvas.width = 1600;
      canvas.height = 1600;
      canvas.style.backgroundColor = "var(--msg-bg)";
      canvas.style.borderRadius = "4px";

      const mapContainer = document.createElement("div");
      mapContainer.style.position = "relative";
      mapContainer.appendChild(canvas);
      this.ui.viewMaps.appendChild(mapContainer);

      // Create a new canvas for the ruler layer
      const rulerLayer = document.createElement("canvas");
      rulerLayer.width = 1600;
      rulerLayer.height = 1600;
      rulerLayer.style.position = "absolute";
      rulerLayer.style.left = "0";
      rulerLayer.style.top = "0";
      rulerLayer.style.pointerEvents = "none"; // Make sure clicks go through to the map canvas
      mapContainer.appendChild(rulerLayer);

      const ctx = canvas.getContext("2d");
      const rulerCtx = rulerLayer.getContext("2d");
      const SCALE = mapData.pixels_per_foot || 15;

      const is_visible_to_player = (x, y) => {
        if (this.activeCharacter === "Human DM") return true;
        for (const area of mapData.explored_areas || []) {
          if (Math.hypot(x - area[0], y - area[1]) <= area[2]) {
            return true;
          }
        }
        return false;
      };

      let bgImageRef = null;
      const drawScene = (bgImg) => {
        bgImageRef = bgImg || bgImageRef;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (bgImageRef) ctx.drawImage(bgImageRef, 0, 0);

        ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
        ctx.lineWidth = 1;
        for (let i = 0; i < canvas.width; i += SCALE * mapData.grid_scale) {
          ctx.beginPath();
          ctx.moveTo(i, 0);
          ctx.lineTo(i, canvas.height);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(0, i);
          ctx.lineTo(canvas.width, i);
          ctx.stroke();
        }

        if (this.activeCharacter !== "Human DM") {
          ctx.fillStyle = "rgba(0, 0, 0, 0.98)";
        } else {
          ctx.fillStyle = "rgba(0, 0, 50, 0.4)";
        }
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        ctx.globalCompositeOperation = "destination-out";
        (mapData.explored_areas || []).forEach((area) => {
          const [x, y, radius] = area;
          ctx.beginPath();
          ctx.arc(x * SCALE, y * SCALE, radius * SCALE, 0, Math.PI * 2);
          ctx.fill();
        });
        ctx.globalCompositeOperation = "source-over";

        const activeWalls = [
          ...(mapData.walls || []),
          ...(mapData.temporary_walls || []),
        ];
        activeWalls.forEach((wall) => {
          if (!is_visible_to_player(wall.start[0], wall.start[1]) && !is_visible_to_player(wall.end[0], wall.end[1])) {
            return;
          }
          ctx.beginPath();
          ctx.moveTo(wall.start[0] * SCALE, wall.start[1] * SCALE);
          ctx.lineTo(wall.end[0] * SCALE, wall.end[1] * SCALE);
          if (!wall.is_solid && wall.is_visible) {
            ctx.strokeStyle = "rgba(40, 167, 69, 0.6)";
            ctx.lineWidth = 4;
          } else if (!wall.is_visible) {
            ctx.strokeStyle = "rgba(0, 150, 255, 0.4)";
            ctx.lineWidth = 2;
          } else {
            ctx.strokeStyle = "rgba(220, 53, 69, 0.8)";
            ctx.lineWidth = 3;
          }
          ctx.stroke();
        });

        entities.forEach((ent) => {
          if (ent.hp <= 0) return;
          if (!is_visible_to_player(ent.x, ent.y)) return;

          const px = ent.x * SCALE;
          const py = ent.y * SCALE;
          const pRadius = (ent.size / 2) * SCALE;

          if (this.activeCharacter !== "Human DM" && !ent.is_pc) {
            let isRevealed = false;
            for (const area of mapData.explored_areas || []) {
              if (Math.hypot(ent.x - area[0], ent.y - area[1]) <= area[2]) {
                isRevealed = true;
                break;
              }
            }
            if (!isRevealed) return;
          }

          ctx.beginPath();
          ctx.arc(px, py, pRadius, 0, Math.PI * 2);
          if (ent.icon_url) {
            if (this.loadedImages[ent.icon_url]) {
              ctx.save();
              ctx.clip();
              ctx.drawImage(
                this.loadedImages[ent.icon_url],
                px - pRadius,
                py - pRadius,
                pRadius * 2,
                pRadius * 2,
              );
              ctx.restore();
            } else {
              const img = new Image();
              img.onload = () => {
                this.loadedImages[ent.icon_url] = img;
                drawScene(bgImageRef);
              };
              img.src = `${this.serverUrl}/vault_media?filepath=${encodeURIComponent(ent.icon_url)}`;
              ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545";
              ctx.fill();
            }
          } else {
            ctx.fillStyle = ent.is_pc ? "#0e639c" : "#dc3545";
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
            if (is_visible_to_player(trap.x, trap.y)) {
                const px = trap.x * SCALE;
                const py = trap.y * SCALE;
                ctx.fillStyle = "red";
                ctx.font = "bold 20px sans-serif";
                ctx.textAlign = "center";
                ctx.fillText("X", px, py);
            }
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

      // --- Waypoint Ruler Logic ---
      canvas.addEventListener("mousedown", (e) => {
        if (this.activeCharacter !== "Human DM") return;
        this.waypoints = []; // Clear waypoints on new mousedown
        rulerLayer.style.pointerEvents = "auto"; // Start listening to mouse events on the ruler layer
        this.isDrawingPath = true;
        const rect = canvas.getBoundingClientRect();
        const x = (e.clientX - rect.left) * (canvas.width / rect.width);
        const y = (e.clientY - rect.top) * (canvas.height / rect.height);
        this.waypoints = [{ x, y }];
      });

      const rulerCtx = rulerLayer.getContext("2d");
      rulerLayer.addEventListener("mousemove", (e) => {
        if (!this.isDrawingPath || !rulerLayer) return;

        const rect = canvas.getBoundingClientRect();
        const x = (e.clientX - rect.left) * (canvas.width / rect.width);
        const y = (e.clientY - rect.top) * (canvas.height / rect.height);

        // Add a new waypoint if the mouse has moved a certain distance
        const lastPoint = this.waypoints[this.waypoints.length - 1];
        if (Math.hypot(x - lastPoint.x, y - lastPoint.y) > 10) {
          this.waypoints.push({ x, y });
        }

        // Draw the path
        rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
        rulerCtx.beginPath();
        rulerCtx.moveTo(this.waypoints[0].x, this.waypoints[0].y);
        for (let i = 1; i < this.waypoints.length; i++) {
          rulerCtx.lineTo(this.waypoints[i].x, this.waypoints[i].y);
        }
        rulerCtx.lineTo(x, y); // Draw to the current mouse position
        rulerCtx.strokeStyle = "rgba(255, 255, 0, 0.8)";
        rulerCtx.lineWidth = 3;
        rulerCtx.stroke();
      });

      rulerLayer.addEventListener("mouseup", async (e) => {
        if (!this.isDrawingPath || !rulerLayer) return;

        this.isDrawingPath = false;
        rulerLayer.style.pointerEvents = "none"; // Stop listening to mouse events
        const rulerCtx = rulerLayer.getContext("2d");
        
        if (this.waypoints.length > 0) {
          const rect = canvas.getBoundingClientRect();
          const x = (e.clientX - rect.left) * (canvas.width / rect.width);
          const y = (e.clientY - rect.top) * (canvas.height / rect.height);
          this.waypoints.push({ x, y });

          const pixelWaypoints = this.waypoints.map(p => [p.x, p.y]);

          try {
            const response = await fetch(`${this.serverUrl}/propose_move`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    entity_name: this.activeCharacter,
                    waypoints: pixelWaypoints,
                    vault_path: this.vaultPath,
                }),
            });

            if (response.ok) {
                const moveData = await response.json();
                this.showMoveConfirmation(moveData, pixelWaypoints, mapData, rulerCtx, SCALE);
            } else {
                const errorText = await response.text();
                this.appendMessage("System", `Error proposing move: ${errorText}`, "red");
                rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
            }
          } catch (err) {
            this.appendMessage("System", `Error proposing move: ${err.message}`, "red");
            rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
          }
        }
        this.waypoints = [];
      });
    }

    showMoveConfirmation(moveData, waypoints, mapData, rulerCtx, SCALE) {
        const dialog = document.createElement("div");
        dialog.className = "move-confirmation-dialog";

        let content = "<h3>Proposed Move</h3>";

        if (!moveData.is_valid) {
            content += "<p style='color: var(--text-error);'>Path is obstructed.</p>";
            if (moveData.alternative_path.length > 0) {
                content += "<p>Suggested alternative:</p>";
                rulerCtx.beginPath();
                rulerCtx.moveTo(moveData.alternative_path[0][0], moveData.alternative_path[0][1]);
                for (let i = 1; i < moveData.alternative_path.length; i++) {
                    rulerCtx.lineTo(moveData.alternative_path[i][0], moveData.alternative_path[i][1]);
                }
                rulerCtx.strokeStyle = 'rgba(0, 255, 255, 0.8)';
                rulerCtx.lineWidth = 3;
                rulerCtx.stroke();
            }
        }

        if (moveData.opportunity_attacks.length > 0) {
            content += `<p>This move will provoke opportunity attacks from: ${moveData.opportunity_attacks.join(", ")}.</p>`;
        }

        if (moveData.traps_triggered.length > 0) {
            content += `<p>This move will trigger the following known traps: ${moveData.traps_triggered.join(", ")}.</p>`;
        }

        content += "<p>Do you want to proceed?</p>";

        const confirmBtn = document.createElement("button");
        confirmBtn.textContent = "Confirm";
        confirmBtn.onclick = () => {
            let moveCommand = "I move";
            for (const point of waypoints) {
                const gridX = Math.round(point.x / SCALE / mapData.grid_scale) * mapData.grid_scale;
                const gridY = Math.round(point.y / SCALE / mapData.grid_scale) * mapData.grid_scale;
                moveCommand += ` to (${gridX}, ${gridY})`;
            }
            this.submitMessage(moveCommand);
            dialog.remove();
            rulerCtx.clearRect(0, 0, rulerCtx.canvas.width, rulerCtx.canvas.height);
        };

        const cancelBtn = document.createElement("button");
        cancelBtn.textContent = "Cancel";
        cancelBtn.onclick = () => {
            dialog.remove();
            rulerCtx.clearRect(0, 0, rulerCtx.canvas.width, rulerCtx.canvas.height);
        };

        dialog.innerHTML = content;
        dialog.appendChild(confirmBtn);
        dialog.appendChild(cancelBtn);

        document.body.appendChild(dialog);
    }

    renderCharacterRadios(lockedCharacters) {
      this.ui.charSelect.innerHTML = "";
      if (!this.availableCharacters.has(this.activeCharacter)) this.activeCharacter = "Human DM";

      this.availableCharacters.forEach((char) => {
        const lbl = document.createElement("label");
        lbl.className = "char-label";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "char-select";
        radio.value = char;
        if (char === this.activeCharacter) radio.checked = true;

        if (char !== "Human DM" && lockedCharacters.includes(char)) {
          radio.disabled = true;
          lbl.style.opacity = "0.5";
          lbl.title = "Character is controlled by another player.";
        }

        radio.addEventListener("change", async (e) => {
          if (e.target.checked) {
            const newChar = e.target.value;
            try {
              const response = await fetch(`${this.serverUrl}/switch_character`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  old_character: this.activeCharacter,
                  new_character: newChar,
                  client_id: this.clientId,
                }),
              });
              if (!response.ok) throw new Error("Lock denied");

              this.activeCharacter = newChar;
              this.updatePerspectiveStyles();
              this.ui.chatInput.placeholder = `Playing as: ${this.activeCharacter}\nWhat do you do?`;
              this.appendMessage(
                "System",
                `Switched to: **${this.activeCharacter}**`,
                "var(--text-muted)",
              );
              this.syncState();
            } catch (err) {
              this.appendMessage("System", `**Error swapping:** ${err.message}`, "red");
              this.renderCharacterRadios(lockedCharacters); // Revert
            }
          }
        });

        lbl.appendChild(radio);
        lbl.appendChild(document.createTextNode(char));
        this.ui.charSelect.appendChild(lbl);
      });
    }

    async submitMessage(message = null) {
      const text = message || this.ui.chatInput.value.trim();
      if (!text || !this.vaultPath) return;

      if (text.startsWith(">") && this.activeCharacter !== "Human DM") {
        this.appendMessage(
          "System",
          "Only the 'Human DM' is allowed to execute OOC commands (>).",
          "red",
        );
        this.ui.chatInput.value = "";
        return;
      }

      this.ui.chatInput.value = "";
      this.ui.chatInput.disabled = true;
      this.appendMessage(this.activeCharacter, text, "var(--accent-hover)");

      const loadingDiv = document.createElement("div");
      loadingDiv.innerHTML = "🎲 <i>DM is thinking...</i>";
      this.ui.chatHistory.appendChild(loadingDiv);
      this.ui.chatHistory.scrollTop = this.ui.chatHistory.scrollHeight;

      try {
        const response = await fetch(`${this.serverUrl}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            character: this.activeCharacter,
            vault_path: this.vaultPath,
            client_id: this.clientId,
            roll_automations: this.rollAutomations,
          }),
        });

        loadingDiv.remove();
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

        const msgDiv = document.createElement("div");
        msgDiv.className = "dm-message";
        msgDiv.innerHTML = `<strong>DM:</strong> <div class="content"></div>`;
        this.ui.chatHistory.appendChild(msgDiv);
        const contentDiv = msgDiv.querySelector(".content");

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
              } catch (e) {}
            }
          }

          if (needsRender) {
            contentDiv.innerHTML = marked.parse(accumulatedText);
            this.ui.chatHistory.scrollTop = this.ui.chatHistory.scrollHeight;
          }
        }
      } catch (e) {
        loadingDiv.remove();
        this.appendMessage(
          "System",
          `**Network Error:** ${e.message}`,
          "var(--text-error)",
        );
      } finally {
        this.ui.chatInput.disabled = false;
        this.ui.chatInput.focus();
      }
    }

    appendMessage(sender, text, color = "white") {
      const msgDiv = document.createElement("div");
      msgDiv.className = "dm-message";
      msgDiv.innerHTML = `<strong style="color: ${color}">${sender}:</strong> <div class="content" style="margin-top: 5px;"></div>`;
      this.ui.chatHistory.appendChild(msgDiv);
      msgDiv.querySelector(".content").innerHTML = marked.parse(text);
      this.ui.chatHistory.scrollTop = this.ui.chatHistory.scrollHeight;
    }

    async startListening() {
      if (this.listenController) this.listenController.abort();
      this.listenController = new AbortController();
      try {
        const res = await fetch(`${this.serverUrl}/listen?client_id=${this.clientId}`, {
          signal: this.listenController.signal,
        });
        if (!res.ok) return;
        this.appendMessage(
          "System",
          "Listening for broadcast events...",
          "var(--text-muted)",
        );

        const reader = res.body.getReader();
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
                    msgDiv = document.createElement("div");
                    msgDiv.className = "dm-message";
                    msgDiv.innerHTML = `<strong style="color: var(--text-muted)">DM (Broadcast):</strong> <div class="content" style="margin-top: 5px;"></div>`;
                    this.ui.chatHistory.appendChild(msgDiv);
                    contentDiv = msgDiv.querySelector(".content");
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
            contentDiv.innerHTML = marked.parse(accumulatedText);
            this.ui.chatHistory.scrollTop = this.ui.chatHistory.scrollHeight;
          }
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          this.appendMessage(
            "System",
            "Listen stream disconnected.",
            "var(--text-error)",
          );
          this.ui.listenCheck.checked = false;
        }
      }
    }

    async fetchCharacters() {
        try {
            const res = await fetch(`${this.serverUrl}/characters`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ vault_path: this.vaultPath }),
            });
            if (res.ok) {
                const data = await res.json();
                this.availableCharacters = new Set(data.characters);
                this.renderCharacterRadios([]);
            }
        } catch (e) {
            console.error("Failed to fetch characters:", e);
        }
    }
}
