const ui = {
  statusIndicator: document.getElementById("status-indicator"),
  vaultInput: document.getElementById("vault-path-input"),
  connectBtn: document.getElementById("connect-btn"),
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
  serverDropdown: document.getElementById("server-dropdown"),
  serverDropdownArrow: document.getElementById("server-dropdown-arrow"),
  viewSheet: document.getElementById("view-sheet"),
  viewMaps: document.getElementById("view-maps"),
  partyList: document.getElementById("party-list"),
};

const clientCore = new DMEngineClientCore({ ui: ui, viewSheet: ui.viewSheet, viewMaps: ui.viewMaps }, "web");

// Load saved server URL from localStorage
const savedServerUrl = localStorage.getItem("dm_server_url_web") || "";
ui.serverUrlInput.value = savedServerUrl;
ui.vaultInput.value = clientCore.vaultPath;
clientCore.updatePerspectiveStyles();

// Track discovered servers
let _discoveredServers = [];
let _dropdownOpen = false;
let _scanInProgress = false;

// Show/hide the server dropdown
function setDropdownOpen(open) {
  _dropdownOpen = open;
  if (ui.serverDropdown) {
    ui.serverDropdown.style.display = open ? "block" : "none";
  }
  if (ui.serverDropdownArrow) {
    ui.serverDropdownArrow.textContent = open ? "▲" : "▼";
  }
}

// Populate the server dropdown with found servers
function populateDropdown(servers) {
  if (!ui.serverDropdown) return;
  ui.serverDropdown.innerHTML = "";

  if (servers.length === 0) {
    ui.serverDropdown.innerHTML = '<div style="padding: 8px 12px; color: #888; font-size: 0.9em;">No servers found</div>';
    return;
  }

  servers.forEach((s, idx) => {
    const item = document.createElement("div");
    item.style.cssText = "padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #333;";
    item.innerHTML = `<div style="font-weight: bold;">${s.server_name}</div><div style="font-size: 0.85em; color: #aaa;">${s.campaign} — ${s.url}</div>`;
    item.addEventListener("click", () => {
      ui.serverUrlInput.value = s.url;
      setDropdownOpen(false);
    });
    item.addEventListener("mouseenter", () => { item.style.background = "#444"; });
    item.addEventListener("mouseleave", () => { item.style.background = ""; });
    ui.serverDropdown.appendChild(item);
  });

  // Add manual entry option
  const manual = document.createElement("div");
  manual.style.cssText = "padding: 8px 12px; cursor: pointer; color: #888; font-size: 0.85em;";
  manual.textContent = "— Type address manually above —";
  manual.addEventListener("click", () => { setDropdownOpen(false); });
  ui.serverDropdown.appendChild(manual);
}

// Network scan - probes common local IPs for DM Engine servers
async function scanForServers() {
  if (_scanInProgress) return;
  _scanInProgress = true;

  ui.serverUrlInput.placeholder = "Scanning...";
  ui.serverDropdown.innerHTML = '<div style="padding: 8px 12px; color: #888; font-size: 0.9em;">Scanning network...</div>';
  setDropdownOpen(true);

  const found = [];
  const seen = new Set();

  // Get the user's local IP to determine the subnet
  let localIp = "";
  try {
    const pc = new RTCPeerConnection({ iceServers: [] });
    pc.createDataChannel("");
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const sdp = offer.sdp || "";
    const match = sdp.match(/(\d+\.\d+\.\d+\.\d+)/);
    if (match) localIp = match[1];
    pc.close();
  } catch (e) {}

  // Build list of IPs to scan
  const ipsToScan = [];
  if (localIp) {
    const parts = localIp.split(".");
    const base = `${parts[0]}.${parts[1]}.${parts[2]}.`;
    for (let i = 1; i < 255; i++) ipsToScan.push(base + i);
  } else {
    const subnets = ["192.168.0", "192.168.1", "192.168.2", "10.0.0", "10.0.1"];
    for (const subnet of subnets) {
      for (let i = 1; i < 255; i++) ipsToScan.push(`${subnet}.${i}`);
    }
  }

  // Probe heartbeat endpoint on each IP
  const batchSize = 50;
  for (let b = 0; b < ipsToScan.length; b += batchSize) {
    if (!_dropdownOpen) break; // Stop if user closed dropdown
    const batch = ipsToScan.slice(b, b + batchSize);
    const promises = batch.map(async (ip) => {
      try {
        const res = await fetch(`http://${ip}:8000/heartbeat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: "scan", character: "Human DM", roll_automations: {} }),
        });
        if (res.ok) {
          const data = await res.json();
          // Deduplicate by server identity (same server may respond on many IPs)
          const key = `${data.server_name || "?"}|${data.campaign || "?"}`;
          if (!seen.has(key)) {
            seen.add(key);
            found.push({
              url: `http://${ip}:8000`,
              campaign: data.campaign || "Unknown",
              server_name: data.server_name || "DM Engine",
            });
          }
        }
      } catch (e) {}
    });
    await Promise.all(promises);

    // Update dropdown progressively so user sees results
    if (found.length > 0 && _dropdownOpen) {
      populateDropdown(found);
    }
  }

  ui.serverUrlInput.placeholder = "192.168.1.x:8000";
  _discoveredServers = found;
  _scanInProgress = false;

  if (_dropdownOpen) {
    populateDropdown(found);
  }
}

// Click on the input itself → scan and show dropdown
ui.serverUrlInput.addEventListener("focus", () => {
  scanForServers();
});

// Close dropdown when clicking outside
document.addEventListener("click", (e) => {
  if (!ui.serverUrlInput.contains(e.target) && !ui.serverDropdown.contains(e.target)) {
    setDropdownOpen(false);
  }
});

ui.connectBtn.addEventListener("click", async () => {
  clientCore.vaultPath = ui.vaultInput.value.trim();
  localStorage.setItem("dm_vault_path", clientCore.vaultPath);
  let serverAddr = ui.serverUrlInput.value.trim().replace(/\/+$/, "");
  // Normalize: ensure http:// prefix so browser treats it as HTTP not file://
  if (!serverAddr.match(/^https?:\/\//i)) {
    serverAddr = "http://" + serverAddr;
  }
  clientCore.serverUrl = serverAddr;
  localStorage.setItem("dm_server_url_web", serverAddr);

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

    // Auto-enable SSE for real-time updates
    clientCore.startListening();

    setDropdownOpen(false);
    ui.chatInput.disabled = false;
    ui.sendBtn.disabled = false;
    clientCore.appendMessage("System", `Connected to ${clientCore.serverUrl}`);
  } catch (e) {
    alert("Failed to connect to server at " + clientCore.serverUrl + "\n\n" + e.message + "\n\nMake sure the DM's server is running.");
    console.error("Connection error:", e);
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
    localStorage.setItem("dm_snap_to_grid", clientCore.snapToGrid);
  });
}

ui.sendBtn.addEventListener("click", () => clientCore.submitMessage());
ui.chatInput.addEventListener("input", () => clientCore.handleTyping());
ui.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    clientCore.submitMessage();
  }
});

ui.serverUrlInput.addEventListener("change", (e) => {
  let serverAddr = e.target.value.trim().replace(/\/+$/, "");
  if (!serverAddr.match(/^https?:\/\//i)) {
    serverAddr = "http://" + serverAddr;
  }
  clientCore.serverUrl = serverAddr;
  localStorage.setItem("dm_server_url_web", serverAddr);
  clientCore.syncState();
});

// Tab Logic
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-view").forEach((v) => v.classList.remove("active"));
    e.target.classList.add("active");
    document.getElementById(e.target.dataset.target).classList.add("active");
  });
});
