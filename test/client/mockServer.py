#!/usr/bin/env python3
"""
Minimal mock DM Engine server for client unit tests.
Handles: /heartbeat, /character_sheet, /map_state, /party_status,
         /roll, /chat (streaming), /listen (SSE)
Uses only Python standard library.
"""
import json
import random
import time
import threading
import http.server
import socketserver
from urllib.parse import parse_qs

PORT = 18000  # Use non-standard port to avoid conflicts
HEARTBEAT_RESPONSE = {
    "protocol_version": 2,
    "locked_characters": [],
    "server_name": "TestDM",
    "campaign": "TestCampaign",
    "party": [
        {
            "name": "Vex",
            "hp": 45,
            "max_hp": 50,
            "current_map": "Town Square",
            "is_online": True,
            "is_active": True,
            "is_typing": False,
            "is_locked": False,
            "locked_by_other": False,
        },
        {
            "name": "Thornwood",
            "hp": 12,
            "max_hp": 40,
            "current_map": "Town Square",
            "is_online": True,
            "is_active": False,
            "is_typing": False,
            "is_locked": False,
            "locked_by_other": False,
        },
        {
            "name": "Rat King",
            "hp": 0,
            "max_hp": 8,
            "current_map": "Sewers",
            "is_online": False,
            "is_active": False,
            "is_typing": False,
            "is_locked": False,
            "locked_by_other": False,
        },
    ],
    "state_changes": [],
    "character_sheet": {
        "sheet": {
            "name": "Vex",
            "hp": 45,
            "max_hp": 50,
            "ac": 16,
            "speed": "30 ft",
            "conditions": ["Prone"],
            "abilities": {"str": 12, "dex": 18, "con": 14, "int": 10, "wis": 13, "cha": 8},
        }
    },
    "map_state": {
        "map_data": {
            "width": 1200,
            "height": 800,
            "walls": [],
            "dm_map_image_path": "",
            "pixels_per_foot": 15,
        },
        "entities": [],
        "known_traps": [],
        "active_paths": [],
    },
}

CHAR_SHEET_RESPONSE = {
    "sheet": {
        "name": "Vex",
        "hp": 45,
        "max_hp": 50,
        "ac": 16,
        "speed": "30 ft",
        "conditions": ["Prone"],
        "abilities": {"str": 12, "dex": 18, "con": 14, "int": 10, "wis": 13, "cha": 8},
    }
}

MAP_STATE_RESPONSE = {
    "map_data": {
        "width": 1200,
        "height": 800,
        "walls": [],
        "dm_map_image_path": "",
        "pixels_per_foot": 15,
    },
    "entities": [
        {
            "entity_uuid": "abc-123",
            "name": "Vex",
            "x": 100,
            "y": 200,
            "z": 0,
            "icon_url": "",
            "size": 5,
            "hp": 45,
            "max_hp": 50,
            "ac": 16,
            "current_map": "Town Square",
            "tags": ["pc"],
        }
    ],
    "known_traps": [],
    "active_paths": [],
}


class MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # Silence server logs in test output

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_sse(self, data_dict, status=200):
        body = json.dumps(data_dict).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(b"data: " + body + b"\n\n")

    def do_GET(self):
        if self.path.startswith("/listen"):
            # SSE endpoint — stream a few events then hang up
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            events = [
                {"msg": "control", "status": "streaming"},
                {"reply": "🎲 DM is preparing the encounter...\n\n", "msg": "narrative", "status": "streaming"},
                {"msg": "control", "status": "done"},
            ]
            for ev in events:
                self.wfile.write(b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n")
                time.sleep(0.05)
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if self.path == "/heartbeat":
            # Support include_full_state flag
            if data.get("include_full_state"):
                resp = dict(HEARTBEAT_RESPONSE)
                resp["character_sheet"] = CHAR_SHEET_RESPONSE
                resp["map_state"] = MAP_STATE_RESPONSE
                self.send_json(resp)
            else:
                self.send_json(HEARTBEAT_RESPONSE)

        elif self.path == "/character_sheet":
            self.send_json(CHAR_SHEET_RESPONSE)

        elif self.path == "/map_state":
            self.send_json(MAP_STATE_RESPONSE)

        elif self.path == "/party_status":
            self.send_json({"party": HEARTBEAT_RESPONSE["party"]})

        elif self.path == "/roll":
            formula = data.get("formula", "1d20").lower()
            # Parse dice formula
            import re
            m = re.match(r"(\d+)d(\d+)([+-])?(\d+)?", formula)
            if not m:
                self.send_json({"error": "Invalid formula"}, 400)
                return
            num = int(m.group(1))
            sides = int(m.group(2))
            mod_op = m.group(3)
            mod_val = int(m.group(4)) if m.group(4) else 0

            # Predictable rolls for testing: all 1s, except d20=20 (crit) and d20=1 (fumble)
            rolls = []
            for i in range(num):
                if sides == 20 and num == 1:
                    rolls.append(20)  # Always roll nat 20 in test
                else:
                    rolls.append(1)  # All others roll 1

            subtotal = sum(rolls)
            if mod_op == "+":
                total = subtotal + mod_val
            elif mod_op == "-":
                total = subtotal - mod_val
            else:
                total = subtotal

            is_crit = sides == 20 and num == 1 and rolls[0] == 20
            is_fumble = sides == 20 and num == 1 and rolls[0] == 1

            mod_display = mod_val if mod_op else 0
            mod_str = f" {mod_op}{mod_val}" if mod_op else ""
            self.send_json({
                "formula": f"{num}d{sides}{mod_str}",
                "reason": data.get("reason", "Roll"),
                "character": data.get("character", "TestChar"),
                "rolls": rolls,
                "modifier": mod_display,
                "modifier_op": mod_op,
                "subtotal": subtotal,
                "total": total,
                "is_crit": is_crit,
                "is_fumble": is_fumble,
                "roll_str": str(rolls)[1:-1],
            })

        elif self.path == "/chat":
            # SSE streaming response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            chunks = [
                {"reply": "The goblin snarls and lunges!\n\n", "msg": "narrative", "status": "streaming"},
                {"reply": "", "msg": "control", "status": "done"},
            ]
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                time.sleep(0.03)

        else:
            self.send_json({"error": "Unknown endpoint"}, 404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(port=PORT):
    server = ThreadedHTTPServer(("127.0.0.1", port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    print(f"Starting mock DM server on port {PORT}...")
    server = start_server()
    print(f"Mock server running on http://127.0.0.1:{PORT}")
    try:
        threading.Event().wait()  # Block forever
    except KeyboardInterrupt:
        server.shutdown()
