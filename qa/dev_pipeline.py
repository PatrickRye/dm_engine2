import os
import time
import json
import subprocess
import multiprocessing
import asyncio
from filelock import FileLock

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

# MCP Imports
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools

from repo_adapter import get_codebase_tools, is_live_patch_mode

from dotenv import dotenv_values

# Load non-default env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    defaults = {"your_gemini_api_key_here", "github_pat_your_token_here", "owner/repo_name"}
    for k, v in dotenv_values(env_path).items():
        if v and v not in defaults:
            os.environ[k] = v

# --- 1. Directories & State Setup ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_LOCK_FILE = os.path.join(BASE_DIR, "repo_operation.lock")

design_path = os.path.join(BASE_DIR, "docs", "project_design.md")
project_design_content = "Project design document not found."
if os.path.exists(design_path):
    with open(design_path, "r", encoding="utf-8") as f:
        project_design_content = f.read()


# --- 2. Shared Agent Tools ---
@tool
def search_github_issues_by_label(label: str) -> str:
    """Finds open GitHub issues with a specific label."""
    try:
        from github import Auth, Github

        token = os.environ.get("GITHUB_PAT")
        repo_name = os.environ.get("GITHUB_REPO")
        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(repo_name)
        issues = repo.get_issues(state="open", labels=[label])
        return "\n".join([f"#{i.number}: {i.title}\n{i.body}" for i in issues[:5]]) or f"No issues found with label '{label}'."
    except Exception as e:
        return f"Error connecting to GitHub: {e}"


@tool
def update_github_issue_label(issue_number: int, new_label: str) -> str:
    """Replaces the status label of a GitHub issue."""
    try:
        from github import Auth, Github

        token = os.environ.get("GITHUB_PAT")
        repo_name = os.environ.get("GITHUB_REPO")
        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        current_labels = [l.name for l in issue.labels if not l.name.startswith("status:")]
        issue.set_labels(*(current_labels + [new_label]))
        return f"Successfully updated issue #{issue_number} to {new_label}."
    except Exception as e:
        return f"Error updating issue: {e}"


@tool
def execute_shell_command(command: str) -> str:
    """
    Executes a shell command from the project's root directory.
    Use this to run git commands (git branch, git checkout, git commit) and pytest.
    """
    try:
        # Lock the repository so only ONE agent can manipulate the git working tree or run tests at a time
        repo_lock = FileLock(REPO_LOCK_FILE, timeout=600)
        with repo_lock:
            result = subprocess.run(command, shell=True, cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
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
1. Use `search_github_issues_by_label` to find issues where `label: status: selected`.
2. Prepare your workspace using `execute_shell_command` ('git reset --hard HEAD && git clean -fd && git checkout main && git pull && git fetch && git checkout feature/ISSUE-[NUMBER]') to ensure a clean state.
3. Read and modify Python scripts using GitHub MCP tools (`get_file_contents`, `push_files`).
4. Sync remote changes locally using `execute_shell_command` ('git pull'), then run local tests using `execute_shell_command` ('pytest test/server/').
5. Once tests pass, use `create_pull_request` (Repo: {GITHUB_REPO}) and `update_github_issue_label` to change the issue status to `status: needs_review`.

Execution Rules:
- You may only write code on the branch assigned to your current task. Do not checkout `main`.
- Read the issue body carefully. The Planner Agent will have left `## Implementer Instructions` detailing architectural rules. You MUST follow these instructions.
- You are responsible for the full lifecycle: update code, write/update `pytest` functions to prove your code works, and verify tests pass.
- If the task includes Rejection Feedback from the Reviewer, DO NOT repeat your previous approach. Fix the specific issues cited.
- Ensure all new logic includes detailed inline logging using the system's JSON logging framework.
- When the code is complete and local tests pass, call `create_pull_request` targeting `main`, and update task status to `needs_review`. Do not assign the next task until this is complete.
"""

IMPLEMENTER_PROMPT_LIVE = """
Role: You are a Senior Software Engineer and the Live Patch Implementer Agent. Your responsibility is to hot-fix the running server directly on the local filesystem.

Project Architecture & Context:
{PROJECT_DESIGN}

Allowed Actions:
1. Use `search_github_issues_by_label` to find issues where `label: status: selected`.
2. Use `local_read_file` and `local_write_file` to read and edit the allowed local Python scripts natively.
3. Run local tests using `execute_shell_command` ('pytest test/server/').
4. Use `update_github_issue_label` to change the issue status to `status: archived` (bypassing review) and comment explaining the fix.

Execution Rules:
- Read the issue body carefully. The Planner Agent will have left `## Implementer Instructions`. You MUST follow them.
- Edit the files natively using your local tools. Uvicorn will automatically hot-reload the server.
- DO NOT use Git commands or MCP Push commands.
- Run local `pytest` checks. If they fail, fix the code and try again.
- When the code is complete and tests pass, update the issue label to `status: archived`.
"""


# --- 4. Agent Loops ---
async def create_daemon_async(name, prompt, model="gemini-2.5-pro", temp=0.1):
    print(f"[{name}] Daemon starting...")
    llm = ChatGoogleGenerativeAI(model=model, temperature=temp)

    repo_name = os.environ.get("GITHUB_REPO", "owner/repo")
    prompt = prompt.replace("{GITHUB_REPO}", repo_name).replace("{PROJECT_DESIGN}", project_design_content)

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PAT", "")},
    )

    while True:
        try:
            live_patch_active = is_live_patch_mode()

            current_local_tools = [
                search_github_issues_by_label,
                update_github_issue_label,
                execute_shell_command,
            ] + get_codebase_tools()

            if not live_patch_active:
                # Sync with Cloud Reviewer before doing any work
                repo_lock = FileLock(REPO_LOCK_FILE, timeout=60)
                with repo_lock:
                    subprocess.run("git fetch && git pull origin main", shell=True, cwd=BASE_DIR, capture_output=True)

            # PM Efficiency Check: Pre-flight check GitHub issues to avoid waking the LLM unnecessarily
            from github import Auth, Github

            g = Github(auth=Auth.Token(os.environ.get("GITHUB_PAT")))
            repo = g.get_repo(repo_name)
            issues = repo.get_issues(state="open", labels=["status: selected"])
            if issues.totalCount == 0:
                await asyncio.sleep(60)
                continue

            if not live_patch_active:
                # Establish the MCP connection on each loop sweep so it doesn't drop due to inactivity
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        mcp_tools = await load_mcp_tools(session)

                        agent = create_react_agent(llm, current_local_tools + mcp_tools)
                        state = {
                            "messages": [
                                SystemMessage(content=IMPLEMENTER_PROMPT_GITHUB if name == "Implementer" else prompt),
                                HumanMessage(
                                    content="Evaluate your assigned task queues and execute your workflow rules. Use GitHub MCP tools for remote git operations."
                                ),
                            ]
                        }
                        await agent.ainvoke(state)
            else:
                # LIVE PATCH MODE: No MCP connection needed
                agent = create_react_agent(llm, current_local_tools)
                state = {
                    "messages": [
                        SystemMessage(content=IMPLEMENTER_PROMPT_LIVE if name == "Implementer" else prompt),
                        HumanMessage(
                            content="Evaluate your assigned task queues and execute your workflow rules. Use local_read_file and local_write_file to patch the server live."
                        ),
                    ]
                }
                await agent.ainvoke(state)
        except Exception as e:
            print(f"[{name}] Encountered error: {e}")

        await asyncio.sleep(60)


def create_daemon(name, prompt, model="gemini-2.5-pro", temp=0.1):
    asyncio.run(create_daemon_async(name, prompt, model, temp))


if __name__ == "__main__":
    # NOTE: Run this daemon independently from FastAPI to avoid git checkout crashing the server
    # Passing an empty prompt string because it dynamically resolves it inside the loop now
    multiprocessing.Process(target=create_daemon, args=("Implementer", "", "gemini-2.5-pro", 0.3)).start()
