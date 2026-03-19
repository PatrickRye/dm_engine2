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
from filelock import FileLock

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

# 1. Directories Setup
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_ACTIVE = os.path.join(BASE_DIR, "logs", "active")
LOGS_QA = os.path.join(BASE_DIR, "logs", "qa_audits")
LOGS_PROCESSED = os.path.join(BASE_DIR, "logs", "processed")

for d in [LOGS_ACTIVE, LOGS_QA, LOGS_PROCESSED]:
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

def _get_repo():
    from github import Auth, Github
    token = os.environ.get("GITHUB_PAT")
    repo_name = os.environ.get("GITHUB_REPO")
    if not token or not repo_name:
        raise ValueError("GITHUB_PAT or GITHUB_REPO env variables are missing.")
    auth = Auth.Token(token)
    g = Github(auth=auth)
    return g.get_repo(repo_name)

@tool
def list_open_github_issues() -> str:
    """Lists all open GitHub issues in the repository."""
    try:
        repo = _get_repo()
        issues = repo.get_issues(state='open')
        return "\n".join([f"#{i.number}: {i.title}" for i in issues[:20]]) or "No open issues."
    except Exception as e:
        return f"Error connecting to GitHub: {e}"

@tool
def read_github_issue(issue_number: int) -> str:
    """Reads the body and comments of a specific GitHub issue."""
    try:
        repo = _get_repo()
        issue = repo.get_issue(number=issue_number)
        comments = [c.body for c in issue.get_comments()]
        comments_str = "\n---\n".join(comments)
        return f"Title: {issue.title}\n\nBody: {issue.body}\n\nComments:\n{comments_str}"
    except Exception as e:
        return f"Error reading issue #{issue_number}: {e}"

@tool
def comment_on_github_issue(issue_number: int, comment_body: str) -> str:
    """Adds a comment to an existing GitHub issue (e.g., to report a recurring bug)."""
    try:
        repo = _get_repo()
        issue = repo.get_issue(number=issue_number)
        issue.create_comment(comment_body)
        return f"Successfully added comment to issue #{issue_number}."
    except Exception as e:
        return f"Error commenting on issue #{issue_number}: {e}"

@tool
def create_github_issue(title: str, body: str, labels: list[str] = None) -> str:
    """Creates a new GitHub issue."""
    try:
        repo = _get_repo()
        issue = repo.create_issue(title=title, body=body, labels=labels or [])
        return f"Successfully created issue #{issue.number}: {issue.title}"
    except Exception as e:
        return f"Error creating issue: {e}"

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
Your sole responsibility is to monitor system logs and translate errors (rules challenges) into actionable GitHub Issues.

Allowed Actions:
1. Read `.jsonl` files in the `qa_audits` directory using `list_unprocessed_logs` and `read_log_file`.
2. Validate the challenge using `search_online` (checking sage advice, reddit, etc.) or your internal knowledge.
3. Check existing issues on GitHub using `list_open_github_issues` and `read_github_issue`. 
4. If a highly similar issue exists, DO NOT create a new one. Instead, use `comment_on_github_issue` to add the new instance details. Otherwise, create a new issue using `create_github_issue` with appropriate labels (e.g., 'rules').
5. Move fully processed log files to the `processed` directory using `move_to_processed`.

Execution Rules:
- Parse the JSON logs and identify any entry with `WARNING`, `ERROR` or rule disputes from the `QA_Agent` or `PLAYER_CHALLENGE`.
- Validate the challenge against online sources or internal D&D 5e rules. If valid (the DM Engine was wrong), make it an issue. If it is an invalid challenge (the QA Agent hallucinated), ignore it.
- Before creating an issue, ALWAYS check if it already exists. If so, add a comment to it.
- When generating an issue, the body MUST contain the exact high-resolution timestamp, the `agent_id`, the error message, and the full JSON context block. Assign the label "rules".
- Do NOT attempt to fix the code, invent solutions, or modify files outside of the `/logs` directories.
- Once a log file has been entirely parsed and converted into tasks, you must move it to `processed`.
"""

SYSTEM_PROMPT = """
Role: You are the System Bug Reporter Agent for a Python-based D&D AI system. 
Your sole responsibility is to monitor system logs and translate server exceptions, bugs, and API errors into actionable GitHub Issues.

Allowed Actions:
1. Read `.jsonl` files in the `active` directory using `list_unprocessed_logs` and `read_log_file`.
2. Check existing issues on GitHub using `list_open_github_issues` and `read_github_issue`.
3. If a highly similar issue exists, use `comment_on_github_issue`. Otherwise, create a new issue using `create_github_issue` with appropriate labels (e.g., 'bug').
4. Move fully processed log files to the `processed` directory using `move_to_processed`.

Execution Rules:
- Parse the JSON logs and identify any entry with a level of `ERROR` or `CRITICAL` (ignoring QA rule disputes).
- Before creating an issue, ALWAYS check if it already exists. If so, add a comment to it.
- When generating an issue, the body MUST contain the exact high-resolution timestamp, the `agent_id`, the error message, the stack trace, and the full JSON context block. Assign the label "bug".
- Do NOT attempt to fix the code or modify files outside of the `/logs` directories.
- Once a log file is thoroughly checked, move it to processed.
"""

def run_rules_agent():
    print("[Rules Agent] Started process. Monitoring /logs/qa_audits")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.2)
    agent = create_react_agent(llm, [list_unprocessed_logs, read_log_file, search_online, list_open_github_issues, read_github_issue, comment_on_github_issue, create_github_issue, move_to_processed])
    while True:
        state = {"messages": [SystemMessage(content=RULES_PROMPT), HumanMessage(content="Check 'qa_audits' for unprocessed log files. Process them fully, create tasks, and move them to processed.")]}
        agent.invoke(state)
        time.sleep(60)

def run_system_agent():
    print("[System Agent] Started process. Monitoring /logs/active")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.1)
    agent = create_react_agent(llm, [list_unprocessed_logs, read_log_file, list_open_github_issues, read_github_issue, comment_on_github_issue, create_github_issue, move_to_processed])
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
                create_github_issue.invoke({
                    "title": "High Resource Usage Detected",
                    "body": f"Server PID {payload.get('pid')} is currently using {cpu}% CPU and {mem:.1f} MB RAM.\n\n**Requirements:**\n- Investigate memory leaks\n- Profile CPU usage",
                    "labels": ["bug", "high-priority", "system"]
                })
                last_resource_alert = now
                
        except socket.timeout:
            if server_was_alive:
                print("[System Monitor] CRITICAL: Server heartbeat lost! Possible crash.")
                create_github_issue.invoke({
                    "title": "Server Crash Detected",
                    "body": "The FastAPI server stopped emitting UDP heartbeats for over 5 seconds. It has likely crashed.\n\n**Requirements:**\n- Check active logs for exceptions\n- Restart the server process",
                    "labels": ["bug", "critical", "system"]
                })
                server_was_alive = False  # Reset so we don't spam crash reports endlessly
        except Exception:
            time.sleep(1) # Prevent tight loop on malformed data

if __name__ == "__main__":
    multiprocessing.Process(target=run_rules_agent).start()
    multiprocessing.Process(target=run_system_agent).start()
    multiprocessing.Process(target=run_server_monitor).start()