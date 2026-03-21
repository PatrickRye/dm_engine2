const ui = {
  statusIndicator: document.getElementById("status-indicator"),
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
  snapGrid: document.getElementById("snap-grid"),
  serverUrlInput: document.getElementById("server-url-input"),
  viewSheet: document.getElementById("view-sheet"),
  viewMaps: document.getElementById("view-maps"),
};

const clientCore = new DMEngineClientCore({ ui: ui, viewSheet: ui.viewSheet, viewMaps: ui.viewMaps }, "web");

// Initialization
ui.vaultInput.value = clientCore.vaultPath;
ui.serverUrlInput.value = clientCore.serverUrl;
clientCore.updatePerspectiveStyles();

ui.connectBtn.addEventListener("click", async () => {
  clientCore.vaultPath = ui.vaultInput.value.trim();
  localStorage.setItem("dm_vault_path", clientCore.vaultPath);
  clientCore.serverUrl = ui.serverUrlInput.value.trim().replace(/\/+$/, "");
  localStorage.setItem("dm_server_url_web", clientCore.serverUrl);

  try {
    // Test the connection immediately
    const res = await fetch(`${clientCore.serverUrl}/characters`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vault_path: clientCore.vaultPath })
    });
    if (!res.ok) throw new Error("Server returned " + res.status);

    await clientCore.fetchCharacters();
    if (!clientCore.pollInterval) {
      clientCore.pollInterval = setInterval(() => clientCore.syncState(), 5000);
      clientCore.syncState();
    }
    ui.chatInput.disabled = false;
    ui.sendBtn.disabled = false;
    clientCore.appendMessage("System", `Connected to Vault: ${clientCore.vaultPath}`);
  } catch (e) {
    alert("Failed to connect to server at " + clientCore.serverUrl + "\n\nCheck the browser console (F12) for details.");
    console.error("Connection error:", e);
  }
});

ui.listenCheck.addEventListener("change", (e) => {
  if (e.target.checked) clientCore.startListening();
  else if (clientCore.listenController) {
    clientCore.listenController.abort();
    clientCore.listenController = null;
  }
});

[ui.autoHidden, ui.autoSaves, ui.autoSkills, ui.autoAttacks].forEach((cb) => {
  cb.addEventListener("change", (e) => {
    const key = e.target.id.replace("auto-", "").replace("-", "_");
    if (key === "hidden") clientCore.rollAutomations.hidden_rolls = e.target.checked;
    if (key === "saves") clientCore.rollAutomations.saving_throws = e.target.checked;
    if (key === "skills") clientCore.rollAutomations.skill_checks = e.target.checked;
    if (key === "attacks") clientCore.rollAutomations.attack_rolls = e.target.checked;
    clientCore.syncState();
  });
});

if (ui.snapGrid) {
  ui.snapGrid.checked = clientCore.snapToGrid;
  ui.snapGrid.addEventListener("change", (e) => {
    clientCore.snapToGrid = e.target.checked;
    localStorage.setItem("dm_snap_to_grid", e.target.checked);
  });
}

ui.sendBtn.addEventListener("click", () => clientCore.submitMessage());
ui.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    clientCore.submitMessage();
  }
});

ui.serverUrlInput.addEventListener("change", (e) => {
  clientCore.serverUrl = e.target.value.trim().replace(/\/+$/, "");
  localStorage.setItem("dm_server_url_web", clientCore.serverUrl);
  clientCore.syncState();
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
