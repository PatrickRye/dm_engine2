// State
let activeCharacter = "Human DM";
const clientId = crypto.randomUUID();
let vaultPath = localStorage.getItem("dm_vault_path") || "";
let serverUrl =
  localStorage.getItem("dm_server_url_web") || "http://127.0.0.1:8000";
let listenController = null;
let pollInterval = null;
let availableCharacters = new Set(["Human DM"]);
const loadedImages = {}; // Cache for map background images
let isMapDragging = false;
let isDrawingPath = false;
let waypoints = [];

const rollAutomations = {
  hidden_rolls: true,
  saving_throws: true,
  skill_checks: true,
  attack_rolls: true,
};

// DOM Elements
const ui = {
  status: document.getElementById("status-indicator"),
  vaultInput: document.getElementById("vault-path-input"),
  connectBtn: document.getElementById("connect-btn"),
  listenCheck: document.getElementById("listen-checkbox"),
  charSelect: document.getElementById("char-select-container"),
  chatHistory: document.getElementById("chat-history"),
  chatInput: document.getElementById("chat-input"),
  sendBtn: document.getElementById("send-btn"),
  autoHidden: document.getElementById("auto-hidden"),
  autoSaves: document.getElementById("auto-saves"),
  autoSkills: document.getElementById("auto-skills"),
  autoAttacks: document.getElementById("auto-attacks"),
  serverUrlInput: document.getElementById("server-url-input"),
  viewSheet: document.getElementById("view-sheet"),
  viewMaps: document.getElementById("view-maps"),
};

// Initialization
ui.vaultInput.value = vaultPath;
ui.serverUrlInput.value = serverUrl;
updatePerspectiveStyles();

ui.connectBtn.addEventListener("click", async () => {
  vaultPath = ui.vaultInput.value.trim();
  localStorage.setItem("dm_vault_path", vaultPath);
  await fetchCharacters();
  if (!pollInterval) {
    pollInterval = setInterval(syncState, 5000);
    syncState();
  }
  ui.chatInput.disabled = false;
  ui.sendBtn.disabled = false;
  appendMessage("System", `Connected to Vault: ${vaultPath}`);
});

ui.listenCheck.addEventListener("change", (e) => {
  if (e.target.checked) startListening();
  else if (listenController) {
    listenController.abort();
    listenController = null;
  }
});

[ui.autoHidden, ui.autoSaves, ui.autoSkills, ui.autoAttacks].forEach((cb) => {
  cb.addEventListener("change", (e) => {
    const key = e.target.id.replace("auto-", "").replace("-", "_");
    if (key === "hidden") rollAutomations.hidden_rolls = e.target.checked;
    if (key === "saves") rollAutomations.saving_throws = e.target.checked;
    if (key === "skills") rollAutomations.skill_checks = e.target.checked;
    if (key === "attacks") rollAutomations.attack_rolls = e.target.checked;
    syncState();
  });
});

ui.sendBtn.addEventListener("click", submitMessage);
ui.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitMessage();
  }
});

ui.serverUrlInput.addEventListener("change", (e) => {
  serverUrl = e.target.value.trim().replace(/\/+$/, "");
  localStorage.setItem("dm_server_url_web", serverUrl);
  syncState();
});

// Tab Logic
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    document
      .querySelectorAll(".tab-btn")
      .forEach((b) => b.classList.remove("active"));
    document
      .querySelectorAll(".tab-view")
      .forEach((v) => v.classList.remove("active"));
    e.target.classList.add("active");
    document.getElementById(e.target.dataset.target).classList.add("active");
  });
});

// Core Functions
async function fetchCharacters() {
  try {
    const res = await fetch(`${serverUrl}/characters`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vault_path: vaultPath }),
    });
    if (res.ok) {
      const data = await res.json();
      availableCharacters = new Set(data.characters);
      renderCharacterRadios([]);
    }
  } catch (e) {
    console.error("Failed to fetch characters:", e);
  }
}

function updatePerspectiveStyles() {
  let styleEl = document.getElementById("dm-perspective-styles");
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = "dm-perspective-styles";
    document.head.appendChild(styleEl);
  }
  styleEl.textContent = `
        .perspective { display: none; margin-bottom: 10px; padding: 10px; border-left: 3px solid #7289da; background: rgba(114, 137, 218, 0.1); border-radius: 4px; }
        .perspective[data-target="ALL"] { display: block; border-left: none; background: transparent; padding: 0; }
        .perspective[data-target="${activeCharacter}"] { display: block; }
    `;
}

async function syncState() {
  if (!vaultPath) return;
  try {
    const response = await fetch(`${serverUrl}/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_id: clientId,
        character: activeCharacter,
        roll_automations: rollAutomations,
      }),
    });

    if (response.ok) {
      const data = await response.json();
      renderCharacterRadios(data.locked_characters || []);
      setConnectionStatus(true);

      fetchCharacterSheet();
      fetchMaps();
    } else {
      setConnectionStatus(false);
    }
  } catch (e) {
    setConnectionStatus(false);
  }
}

function setConnectionStatus(isLive) {
  if (isLive) {
    ui.status.textContent = "🟢 Live";
    ui.status.style.color = "var(--text-success)";
  } else {
    ui.status.textContent = "🔴 Disconnected";
    ui.status.style.color = "var(--text-error)";
  }
}

async function fetchCharacterSheet() {
  try {
    const res = await fetch(`${serverUrl}/character_sheet`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vault_path: vaultPath,
        character: activeCharacter,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      renderCharacterSheet(data);
    }
  } catch (e) {}
}

async function fetchMaps() {
  if (isMapDragging) return;
  try {
    const res = await fetch(`${serverUrl}/map_state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vault_path: vaultPath }),
    });
    if (res.ok) {
      const data = await res.json();
      if (!isMapDragging) renderMaps(data);
    }
  } catch (e) {}
}

function renderCharacterSheet(data) {
  if (!data || data.error) {
    ui.viewSheet.innerHTML = `<div style="color:var(--text-error);">${data ? data.error : "Failed to load sheet."}</div>`;
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

  ui.viewSheet.innerHTML = `
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

function renderMaps(data) {
  ui.viewMaps.innerHTML = "";
  if (
    !data ||
    !data.map_data ||
    (!data.map_data.walls.length && !data.map_data.dm_map_image_path)
  ) {
    ui.viewMaps.innerHTML =
      "<p style='color:var(--text-muted);'>No active maps loaded in engine.</p>";
    return;
  }

  const mapData = data.map_data;
  const entities = data.entities || [];
  const knownTraps = data.known_traps || [];

  let imagePath = null;
  if (activeCharacter === "Human DM") {
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
  ui.viewMaps.appendChild(mapContainer);

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
    if (activeCharacter === "Human DM") return true;
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

    if (activeCharacter !== "Human DM") {
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

      if (activeCharacter !== "Human DM" && !ent.is_pc) {
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
        if (loadedImages[ent.icon_url]) {
          ctx.save();
          ctx.clip();
          ctx.drawImage(
            loadedImages[ent.icon_url],
            px - pRadius,
            py - pRadius,
            pRadius * 2,
            pRadius * 2,
          );
          ctx.restore();
        } else {
          const img = new Image();
          img.onload = () => {
            loadedImages[ent.icon_url] = img;
            drawScene(bgImageRef);
          };
          img.src = `${serverUrl}/vault_media?filepath=${encodeURIComponent(ent.icon_url)}`;
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
    if (loadedImages[imagePath]) {
      drawScene(loadedImages[imagePath]);
    } else {
      const img = new Image();
      img.onload = () => {
        loadedImages[imagePath] = img;
        drawScene(img);
      };
      img.src = `${serverUrl}/vault_media?filepath=${encodeURIComponent(imagePath)}`;
    }
  } else {
    drawScene(null);
  }

  // --- Waypoint Ruler Logic ---
  canvas.addEventListener("mousedown", (e) => {
    if (activeCharacter !== "Human DM") return;
    waypoints = []; // Clear waypoints on new mousedown
    rulerLayer.style.pointerEvents = "auto"; // Start listening to mouse events on the ruler layer
    isDrawingPath = true;
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const y = (e.clientY - rect.top) * (canvas.height / rect.height);
    waypoints = [{ x, y }];
  });

  const rulerCtx = rulerLayer.getContext("2d");
  rulerLayer.addEventListener("mousemove", (e) => {
    if (!isDrawingPath || !rulerLayer) return;

    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const y = (e.clientY - rect.top) * (canvas.height / rect.height);

    // Add a new waypoint if the mouse has moved a certain distance
    const lastPoint = waypoints[waypoints.length - 1];
    if (Math.hypot(x - lastPoint.x, y - lastPoint.y) > 10) {
      waypoints.push({ x, y });
    }

    // Draw the path
    rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
    rulerCtx.beginPath();
    rulerCtx.moveTo(waypoints[0].x, waypoints[0].y);
    for (let i = 1; i < waypoints.length; i++) {
      rulerCtx.lineTo(waypoints[i].x, waypoints[i].y);
    }
    rulerCtx.lineTo(x, y); // Draw to the current mouse position
    rulerCtx.strokeStyle = "rgba(255, 255, 0, 0.8)";
    rulerCtx.lineWidth = 3;
    rulerCtx.stroke();
  });

  rulerLayer.addEventListener("mouseup", async (e) => {
    if (!isDrawingPath || !rulerLayer) return;

    isDrawingPath = false;
    rulerLayer.style.pointerEvents = "none"; // Stop listening to mouse events
    const rulerCtx = rulerLayer.getContext("2d");
    
    if (waypoints.length > 0) {
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (canvas.width / rect.width);
      const y = (e.clientY - rect.top) * (canvas.height / rect.height);
      waypoints.push({ x, y });

      const pixelWaypoints = waypoints.map(p => [p.x, p.y]);

      try {
        const response = await fetch(`${serverUrl}/propose_move`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                entity_name: activeCharacter,
                waypoints: pixelWaypoints,
                vault_path: vaultPath,
            }),
        });

        if (response.ok) {
            const moveData = await response.json();
            showMoveConfirmation(moveData, pixelWaypoints, mapData, rulerCtx, SCALE);
        } else {
            const errorText = await response.text();
            appendMessage("System", `Error proposing move: ${errorText}`, "red");
            rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
        }
      } catch (err) {
        appendMessage("System", `Error proposing move: ${err.message}`, "red");
        rulerCtx.clearRect(0, 0, rulerLayer.width, rulerLayer.height);
      }
    }
    waypoints = [];
  });
}

function showMoveConfirmation(moveData, waypoints, mapData, rulerCtx, SCALE) {
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
        submitMessage(moveCommand);
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

function renderCharacterRadios(lockedCharacters) {
  ui.charSelect.innerHTML = "";
  if (!availableCharacters.has(activeCharacter)) activeCharacter = "Human DM";

  availableCharacters.forEach((char) => {
    const lbl = document.createElement("label");
    lbl.className = "char-label";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "char-select";
    radio.value = char;
    if (char === activeCharacter) radio.checked = true;

    if (char !== "Human DM" && lockedCharacters.includes(char)) {
      radio.disabled = true;
      lbl.style.opacity = "0.5";
      lbl.title = "Character is controlled by another player.";
    }

    radio.addEventListener("change", async (e) => {
      if (e.target.checked) {
        const newChar = e.target.value;
        try {
          const response = await fetch(`${serverUrl}/switch_character`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              old_character: activeCharacter,
              new_character: newChar,
              client_id: clientId,
            }),
          });
          if (!response.ok) throw new Error("Lock denied");

          activeCharacter = newChar;
          updatePerspectiveStyles();
          ui.chatInput.placeholder = `Playing as: ${activeCharacter}\nWhat do you do?`;
          appendMessage(
            "System",
            `Switched to: **${activeCharacter}**`,
            "var(--text-muted)",
          );
          syncState();
        } catch (err) {
          appendMessage("System", `**Error swapping:** ${err.message}`, "red");
          renderCharacterRadios(lockedCharacters); // Revert
        }
      }
    });

    lbl.appendChild(radio);
    lbl.appendChild(document.createTextNode(char));
    ui.charSelect.appendChild(lbl);
  });
}

async function submitMessage(message = null) {
  const text = message || ui.chatInput.value.trim();
  if (!text || !vaultPath) return;

  if (text.startsWith(">") && activeCharacter !== "Human DM") {
    appendMessage(
      "System",
      "Only the 'Human DM' is allowed to execute OOC commands (>).",
      "red",
    );
    ui.chatInput.value = "";
    return;
  }

  ui.chatInput.value = "";
  ui.chatInput.disabled = true;
  appendMessage(activeCharacter, text, "var(--accent-hover)");

  const loadingDiv = document.createElement("div");
  loadingDiv.innerHTML = "🎲 <i>DM is thinking...</i>";
  ui.chatHistory.appendChild(loadingDiv);
  ui.chatHistory.scrollTop = ui.chatHistory.scrollHeight;

  try {
    const response = await fetch(`${serverUrl}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        character: activeCharacter,
        vault_path: vaultPath,
        client_id: clientId,
        roll_automations: rollAutomations,
      }),
    });

    loadingDiv.remove();
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

    const msgDiv = document.createElement("div");
    msgDiv.className = "dm-message";
    msgDiv.innerHTML = `<strong>DM:</strong> <div class="content"></div>`;
    ui.chatHistory.appendChild(msgDiv);
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
        ui.chatHistory.scrollTop = ui.chatHistory.scrollHeight;
      }
    }
  } catch (e) {
    loadingDiv.remove();
    appendMessage(
      "System",
      `**Network Error:** ${e.message}`,
      "var(--text-error)",
    );
  } finally {
    ui.chatInput.disabled = false;
    ui.chatInput.focus();
  }
}

function appendMessage(sender, text, color = "white") {
  const msgDiv = document.createElement("div");
  msgDiv.className = "dm-message";
  msgDiv.innerHTML = `<strong style="color: ${color}">${sender}:</strong> <div class="content" style="margin-top: 5px;"></div>`;
  ui.chatHistory.appendChild(msgDiv);
  msgDiv.querySelector(".content").innerHTML = marked.parse(text);
  ui.chatHistory.scrollTop = ui.chatHistory.scrollHeight;
}

async function startListening() {
  if (listenController) listenController.abort();
  listenController = new AbortController();
  try {
    const res = await fetch(`${serverUrl}/listen?client_id=${clientId}`, {
      signal: listenController.signal,
    });
    if (!res.ok) return;
    appendMessage(
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
                ui.chatHistory.appendChild(msgDiv);
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
        ui.chatHistory.scrollTop = ui.chatHistory.scrollHeight;
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      appendMessage(
        "System",
        "Listen stream disconnected.",
        "var(--text-error)",
      );
      ui.listenCheck.checked = false;
    }
  }
}
