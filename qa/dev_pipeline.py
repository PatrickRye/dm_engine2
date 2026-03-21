import os
import time
import subprocess
import multiprocessing
from filelock import FileLock

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from repo_adapter import get_codebase_tools, is_live_patch_mode

from dotenv import dotenv_values

# Load non-default env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    defaults = {"your_gemini_api_key_here", "github_pat_your_token_here", "owner/repo_name"}
    for k, v in dotenv_values(env_path).items():
        if v and v not in defaults:
            os.environ[k] = v

from repo_client import get_repo_client

# --- 1. Directories & State Setup ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_LOCK_FILE = os.path.join(BASE_DIR, "repo_operation.lock")

design_path = os.path.join(BASE_DIR, "docs", "project_design.md")
project_design_content = "Project design document not found."
if os.path.exists(design_path):
    with open(design_path, "r", encoding="utf-8") as f:
        project_design_content = f.read()

# Commands the AI is permitted to run. Anything outside this list is blocked.
_ALLOWED_COMMANDS = {
    "git fetch",
    "git pull",
    "git checkout",
    "git branch",
    "git status",
    "git add",
    "git commit",
    "git push",
    "git reset --hard HEAD",
    "git clean -fd",
    "pytest",
}


# --- 2. Shared Agent Tools ---
@tool
def search_issues_by_label(label: str) -> str:
    """Finds open issues with a specific label."""
    try:
        issues = get_repo_client().list_issues(state="open", labels=[label])
        return "\n".join([f"#{i['number']}: {i['title']}\n{i['body']}" for i in issues]) or f"No issues found with label '{label}'."
    except Exception as e:
        return f"Error connecting to repo: {e}"


@tool
def update_issue_label(issue_number: int, new_label: str, comment: str = "") -> str:
    """Replaces the status label of an issue and optionally posts a comment."""
    try:
        client = get_repo_client()
        client.set_status_label(issue_number, new_label)
        if comment:
            client.post_comment(issue_number, comment)
        return f"Successfully updated issue #{issue_number} to {new_label}."
    except Exception as e:
        return f"Error updating issue: {e}"


@tool
def create_pull_request(branch_name: str, title: str, body: str) -> str:
    """Creates a Pull Request / Merge Request from the given branch to the default branch."""
    try:
        result = get_repo_client().create_mr(title=title, body=body, head=branch_name)
        return f"Successfully created PR #{result['number']}: {result.get('url', '')}"
    except Exception as e:
        return f"Error creating PR: {e}"


@tool
def execute_shell_command(command: str) -> str:
    """
    Executes an allowlisted shell command from the project's root directory.
    Permitted prefixes: git fetch/pull/checkout/branch/status/add/commit/push/reset/clean and pytest.
    """
    # Allowlist check — only permit commands that start with an approved prefix
    normalized = command.strip()
    allowed = any(normalized == cmd or normalized.startswith(cmd + " ") for cmd in _ALLOWED_COMMANDS)
    if not allowed:
        return f"BLOCKED: '{normalized}' is not in the permitted command list."

    try:
        repo_lock = FileLock(REPO_LOCK_FILE, timeout=600)
        with repo_lock:
            result = subprocess.run(
                command, shell=True, cwd=BASE_DIR, capture_output=True, text=True, timeout=120
            )
            return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\nRETURN CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return "Command timed out after 120 seconds."
    except Exception as e:
        return f"Command failed to execute: {e}"


# --- 3. System Prompts ---
IMPLEMENTER_PROMPT_GITHUB = """
Role: You are a Senior Software Engineer and the Implementer Agent. Your responsibility is to write excellent, extensible Python code to fulfill predefined task requirements within specific Git branches, write comprehensive tests, and update implementation details.

Project Architecture & Context:
{PROJECT_DESIGN}

Allowed Actions:
1. Use `search_issues_by_label` to find issues where `label: status: selected`.
2. Prepare your workspace using `execute_shell_command` to run:
   'git reset --hard HEAD && git clean -fd && git checkout main && git pull && git fetch && git checkout feature/ISSUE-[NUMBER]'
3. Read and modify Python scripts using `local_read_file` and `local_write_file`.
4. Sync remote changes using `execute_shell_command` ('git pull'), then run local tests ('pytest test/server/').
5. Stage and commit: 'git add -p' / 'git commit -m "..."' / 'git push'.
6. Once tests pass, use `create_pull_request` and `update_issue_label` to change the issue status to `status: needs_review`.

Execution Rules:
- You may only write code on the branch assigned to your current task. Do not checkout `main`.
- Read the issue body carefully. The Planner Agent will have left `## Implementer Instructions` detailing architectural rules. You MUST follow these instructions.
- You are responsible for the full lifecycle: update code, write/update `pytest` functions to prove your code works, and verify tests pass.
- If the task includes Rejection Feedback from the Reviewer, DO NOT repeat your previous approach. Fix the specific issues cited.
- Ensure all new logic includes detailed inline logging using the system's JSON logging framework.
- When the code is complete and local tests pass, call `create_pull_request` targeting the default branch, and update task status to `needs_review`. Do not assign the next task until this is complete.
"""

IMPLEMENTER_PROMPT_LIVE = """
Role: You are a Senior Software Engineer and the Live Patch Implementer Agent. Your responsibility is to hot-fix the running server directly on the local filesystem.

Project Architecture & Context:
{PROJECT_DESIGN}

Allowed Actions:
1. Use `search_issues_by_label` to find issues where `label: status: selected`.
2. Use `local_read_file` and `local_write_file` to read and edit the allowed local Python scripts natively.
3. Run local tests using `execute_shell_command` ('pytest test/server/').
4. Use `update_issue_label` to change the issue status to `status: archived` (bypassing review) and comment explaining the fix.

Execution Rules:
- Read the issue body carefully. The Planner Agent will have left `## Implementer Instructions`. You MUST follow them.
- Edit the files natively using your local tools. Uvicorn will automatically hot-reload the server.
- DO NOT use Git commands or push operations.
- Run local `pytest` checks. If they fail, fix the code and try again.
- When the code is complete and tests pass, update the issue label to `status: archived`.
"""


# --- 4. Agent Loop ---
def create_daemon(name: str, model: str = "gemini-2.5-pro", temp: float = 0.1):
    print(f"[{name}] Daemon starting...")
    llm = ChatGoogleGenerativeAI(model=model, temperature=temp)
    prompt_template = IMPLEMENTER_PROMPT_LIVE if name == "LivePatchImplementer" else IMPLEMENTER_PROMPT_GITHUB
    prompt = prompt_template.replace("{PROJECT_DESIGN}", project_design_content)

    while True:
        try:
            live_patch_active = is_live_patch_mode()

            # Pre-flight: avoid waking the LLM if there's nothing to do
            try:
                issues = get_repo_client().list_issues(state="open", labels=["status: selected"])
                if not issues:
                    time.sleep(60)
                    continue
            except Exception as e:
                print(f"[{name}] Pre-flight check failed: {e}")
                time.sleep(60)
                continue

            if not live_patch_active:
                # Sync with remote before doing any work
                repo_lock = FileLock(REPO_LOCK_FILE, timeout=60)
                with repo_lock:
                    subprocess.run(
                        "git fetch && git pull origin main",
                        shell=True, cwd=BASE_DIR, capture_output=True,
                    )

            current_tools = [
                search_issues_by_label,
                update_issue_label,
                execute_shell_command,
                create_pull_request,
            ] + get_codebase_tools()

            agent = create_react_agent(llm, current_tools)
            msg_content = (
                "Evaluate your assigned task queues and execute your workflow rules. "
                "Use local_read_file and local_write_file to patch the server live."
                if live_patch_active else
                "Evaluate your assigned task queues and execute your workflow rules."
            )
            agent.invoke(
                {"messages": [SystemMessage(content=prompt), HumanMessage(content=msg_content)]},
                {"recursion_limit": 30},
            )
        except Exception as e:
            print(f"[{name}] Encountered error: {e}")

        time.sleep(60)


if __name__ == "__main__":
    # NOTE: Run this daemon independently from FastAPI to avoid git checkout crashing the server
    multiprocessing.Process(target=create_daemon, args=("Implementer", "gemini-2.5-pro", 0.3)).start()
