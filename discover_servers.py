#!/usr/bin/env python3
"""Server discovery utility - Run this to find DM Engine servers on your LAN."""

import socket
import json
import sys
import time

DISCOVERY_PORT = 9998
BUFFER_SIZE = 1024


def discover_servers(timeout=3.0):
    """Listen for DM Engine server broadcasts on the local network."""
    servers = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.bind(("", DISCOVERY_PORT))
        print(f"Listening for DM Engine servers on port {DISCOVERY_PORT}...")
        print("-" * 50)

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                info = json.loads(data.decode("utf-8"))
                info["addr"] = addr[0]
                if not any(s["addr"] == addr[0] for s in servers):
                    servers.append(info)
                    print(f"Found server at {addr[0]}:{info.get('port', 8000)}")
                    print(f"  Campaign: {info.get('campaign', 'Unknown')}")
                    print(f"  Vault: {info.get('vault_path', 'N/A')}")
                    print()
            except socket.timeout:
                break
            except json.JSONDecodeError:
                pass

    except PermissionError:
        print(f"Error: Need root/admin privileges to listen on port {DISCOVERY_PORT}")
        print("Try: sudo python discover_servers.py")
        return []
    except Exception as e:
        print(f"Error: {e}")
        return []

    return servers


def main():
    print("=" * 50)
    print("DM Engine Server Discovery")
    print("=" * 50)
    print()

    servers = discover_servers(timeout=3.0)

    if not servers:
        print("No servers found. Make sure DM Engine servers are running on your network.")
        sys.exit(1)

    print("-" * 50)
    print(f"Found {len(servers)} server(s):")
    for i, s in enumerate(servers, 1):
        addr = s.get("addr", "unknown")
        port = s.get("port", 8000)
        campaign = s.get("campaign", "Unknown")
        print(f"  {i}. {campaign} at http://{addr}:{port}")


if __name__ == "__main__":
    main()
