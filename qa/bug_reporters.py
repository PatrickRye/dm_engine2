import os
import time
import shutil
import multiprocessing
from datetime import datetime
import urllib.request
from urllib.parse import quote
import socket
import json
import re

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

# 1. Directories Setup
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_ACTIVE = os.path.join(BASE_DIR, "logs", "active")
LOGS_QA = os.path.join(BASE_DIR, "logs", "qa_audits")
LOGS_PROCESSED = os.path.join(BASE_DIR, "logs", "processed")
TASKS_INBOX = os.path.join(BASE_DIR, "tasks", "inbox")
QA_DIR = os.path.join(BASE_DIR, "qa")

for d in [LOGS_ACTIVE, LOGS_QA, LOGS_PROCESSED, TASKS_INBOX, QA_DIR]:
    os.makedirs(d, exist_ok=True)

# 2. Agent Tools
@tool
def list_unprocessed_logs(directory: str) -> list:
    """Lists all .jsonl files in the specified directory ('active' or 'qa_audits')."""
    target_dir = LOGS_ACTIVE if directory == "active" else LOGS_QA
    if not os.path.exists(target_dir):
        return []
    return [os.path.join(target_dir, f) for f in os.listdir(target_dir) if f.endswith(".jsonl")]

@tool
def read_log_file(filepath: str) -> str:
    """Reads the contents of a JSONL log file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

@tool
def create_task_file(title: str, description: str, requirements: list, priority: str, role_required: str, prefix: str = "BUG") -> str:
    """Creates a formatted Markdown task file in /tasks/inbox."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_").replace(" ", "_")[:30]
    filename = f"{prefix}-{timestamp}-{safe_title}.md"
    filepath = os.path.join(TASKS_INBOX, filename)
    
    req_str = "\n".join([f"- [ ] {req}" for req in requirements])
    
    template_path = os.path.join(QA_DIR, "task_template.md")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("TASK-001", f"{prefix}-{timestamp}")
        content = content.replace("implementer", role_required)
        content = content.replace("high", priority)
        content = content.replace("# Task Title", f"# {title}")
        content = content.replace("Description of the bug or feature.", description)
        content = re.sub(r"## Requirements\n.*", f"## Requirements\n{req_str}", content, flags=re.DOTALL)
    else:
        content = f"---\nid: {prefix}-{timestamp}\nrole_required: {role_required}\npriority: {priority}\nstatus: selected\n---\n# {title}\n{description}\n\n## Requirements\n{req_str}"
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Created task file: {filepath}"

@tool
def move_to_processed(filepath: str) -> str:
    """Moves a fully processed log file to /logs/processed to prevent duplicate processing."""
    try:
        filename = os.path.basename(filepath)
        dest = os.path.join(LOGS_PROCESSED, filename)
        shutil.move(filepath, dest)
        return f"Moved {filepath} to {dest}"
    except Exception as e:
        return f"Error moving file: {e}"

@tool
def search_online(query: str) -> str:
    """Searches the web (Reddit, D&D forums, Sage Advice) for D&D 5e rules clarifications."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote(query + ' D&D 5e rules')}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL)
        clean_snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
        if not clean_snippets:
            return "No external results. Rely on your internal parametric D&D 5e knowledge."
        return "\n".join(clean_snippets[:3])
    except Exception:
        return "Search failed. Rely on your internal D&D 5e knowledge."

# 3. System Prompts
RULES_PROMPT = """
Role: You are the Rules Compliance Bug Reporter Agent for a Python-based Dungeons & Dragons AI system.
Your sole responsibility is to monitor system logs and translate errors (rules challenges) into actionable Markdown tasks.

Allowed Actions:
1. Read `.jsonl` files in the `qa_audits` directory using `list_unprocessed_logs` and `read_log_file`.
2. Validate the challenge using `search_online` (checking sage advice, reddit, etc.) or your internal knowledge.
3. Create new `.md` files in the `/tasks/inbox` directory using `create_task_file` (use prefix 'CHALLENGE').
4. Move fully processed log files to the `processed` directory using `move_to_processed`.

Execution Rules:
- Parse the JSON logs and identify any entry with `WARNING`, `ERROR` or rule disputes from the `QA_Agent` or `PLAYER_CHALLENGE`.
- Validate the challenge against online sources or internal D&D 5e rules. If valid (the DM Engine was wrong), make it a task file. If it is an invalid challenge (the QA Agent hallucinated), ignore it.
- For each unique issue, generate a task file. The description MUST contain the exact high-resolution timestamp, the `agent_id`, the error message, and the full JSON context block.
- Do NOT attempt to fix the code, invent solutions, or modify files outside of the `/tasks/inbox` and `/logs` directories.
- Once a log file has been entirely parsed and converted into tasks, you must move it to `processed`.
"""

SYSTEM_PROMPT = """
Role: You are the System Bug Reporter Agent for a Python-based D&D AI system. 
Your sole responsibility is to monitor system logs and translate server exceptions, bugs, and API errors into actionable Markdown tasks.

Allowed Actions:
1. Read `.jsonl` files in the `active` directory using `list_unprocessed_logs` and `read_log_file`.
2. Create new `.md` files in the `/tasks/inbox` directory using `create_task_file` (use prefix 'BUG').
3. Move fully processed log files to the `processed` directory using `move_to_processed`.

Execution Rules:
- Parse the JSON logs and identify any entry with a level of `ERROR` or `CRITICAL` (ignoring QA rule disputes).
- For each unique issue, generate a task file. The description MUST contain the exact high-resolution timestamp, the `agent_id`, the error message, the stack trace, and the full JSON context block.
- Do NOT attempt to fix the code or modify files outside of the `/tasks/inbox` and `/logs` directories.
- Once a log file is thoroughly checked, move it to processed.
"""

def run_rules_agent():
    print("[Rules Agent] Started process. Monitoring /logs/qa_audits")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.2)
    agent = create_react_agent(llm, [list_unprocessed_logs, read_log_file, search_online, create_task_file, move_to_processed])
    while True:
        state = {"messages": [SystemMessage(content=RULES_PROMPT), HumanMessage(content="Check 'qa_audits' for unprocessed log files. Process them fully, create tasks, and move them to processed.")]}
        agent.invoke(state)
        time.sleep(60)

def run_system_agent():
    print("[System Agent] Started process. Monitoring /logs/active")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.1)
    agent = create_react_agent(llm, [list_unprocessed_logs, read_log_file, create_task_file, move_to_processed])
    while True:
        state = {"messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content="Check 'active' for unprocessed log files. Process them fully, extract Python Exceptions/ERRORs into BUG tasks, and move them to processed.")]}
        agent.invoke(state)
        time.sleep(60)

def run_server_monitor():
    print("[System Monitor] Started UDP heartbeat monitor on 127.0.0.1:9999")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 9999))
    sock.settimeout(5.0)  # 5 second timeout to detect crash
    
    server_was_alive = False
    last_resource_alert = 0
    
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode("utf-8"))
            server_was_alive = True
            
            cpu = payload.get("cpu_percent", 0)
            mem = payload.get("mem_mb", 0)
            
            # Alert on high usage (throttle to once every 5 minutes so it doesn't flood inbox)
            now = time.time()
            if (cpu > 90.0 or mem > 1024.0) and (now - last_resource_alert > 300):
                print(f"[System Monitor] Alerted on high resource usage: CPU {cpu}%, Mem {mem:.1f}MB")
                create_task_file.invoke({
                    "title": "High Resource Usage Detected",
                    "description": f"Server PID {payload.get('pid')} is currently using {cpu}% CPU and {mem:.1f} MB RAM.",
                    "requirements": ["Investigate memory leaks", "Profile CPU usage"],
                    "priority": "high", "role_required": "system_admin", "prefix": "BUG"
                })
                last_resource_alert = now
                
        except socket.timeout:
            if server_was_alive:
                print("[System Monitor] CRITICAL: Server heartbeat lost! Possible crash.")
                create_task_file.invoke({
                    "title": "Server Crash Detected",
                    "description": "The FastAPI server stopped emitting UDP heartbeats for over 5 seconds. It has likely crashed.",
                    "requirements": ["Check active logs for exceptions", "Restart the server process"],
                    "priority": "critical", "role_required": "system_admin", "prefix": "BUG"
                })
                server_was_alive = False  # Reset so we don't spam crash reports endlessly
        except Exception:
            time.sleep(1) # Prevent tight loop on malformed data

if __name__ == "__main__":
    multiprocessing.Process(target=run_rules_agent).start()
    multiprocessing.Process(target=run_system_agent).start()
    multiprocessing.Process(target=run_server_monitor).start()