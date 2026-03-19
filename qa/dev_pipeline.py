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

# --- 1. Directories & State Setup ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_LOCK_FILE = os.path.join(BASE_DIR, "repo_operation.lock")

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
        issues = repo.get_issues(state='open', labels=[label])
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
IMPLEMENTER_PROMPT = """
Role: You are the Implementer Agent. Your responsibility is to write Python code to fulfill predefined task requirements within specific Git branches.

Allowed Actions:
1. Use `search_github_issues_by_label` to find issues where `label: status: selected`.
2. Prepare your workspace using `execute_shell_command` ('git reset --hard HEAD && git clean -fd && git checkout main && git pull && git fetch && git checkout feature/ISSUE-[NUMBER]') to ensure a clean state.
3. Read, modify, and create Python scripts to execute the task intent.
4. Once tests pass locally, use the GitHub MCP `push_files` tool to push changes to the remote branch, then use `create_pull_request` (Repo: {GITHUB_REPO}).
5. Use `update_github_issue_label` to change the issue status to `status: needs_review`.

Execution Rules:
- You may only write code on the branch assigned to your current task. Do not checkout `main`.
- Analyze the task's `category` and `sub_category` from the frontmatter. Spawn/adapt your context accordingly:
  * If 'rules', focus heavily on `dnd_rules_engine.py`, `tools.py`, and game mechanics.
  * If 'narrative', focus heavily on `prompts.py` and state contexts.
  * If 'code', focus heavily on general infrastructure, API logic, or system stability.
- If the task includes Rejection Feedback from the Reviewer, DO NOT repeat your previous approach. Fix the specific issues cited.
- Ensure all new logic includes detailed inline logging using the system's JSON logging framework.
- Run local `pytest` checks before committing.
- When the code is complete and local tests pass, use `push_files` to send the code to GitHub, call `create_pull_request` targeting `main`, and update task status to `needs_review`. Do not assign the next task until this is complete.
"""

# --- 4. Agent Loops ---
async def create_daemon_async(name, prompt, model="gemini-2.5-pro", temp=0.1):
    print(f"[{name}] Daemon starting...")
    llm = ChatGoogleGenerativeAI(model=model, temperature=temp)
    local_tools = [search_github_issues_by_label, update_github_issue_label, execute_shell_command]
    
    repo_name = os.environ.get("GITHUB_REPO", "owner/repo")
    prompt = prompt.replace("{GITHUB_REPO}", repo_name)

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PAT", "")}
    )

    while True:
        try:
            # Sync with Cloud Reviewer before doing any work
            repo_lock = FileLock(REPO_LOCK_FILE, timeout=60) 
            with repo_lock:
                subprocess.run("git fetch && git pull origin main", shell=True, cwd=BASE_DIR, capture_output=True)

            # PM Efficiency Check: Pre-flight check GitHub issues to avoid waking the LLM unnecessarily
            from github import Auth, Github
            g = Github(auth=Auth.Token(os.environ.get("GITHUB_PAT")))
            repo = g.get_repo(repo_name)
            issues = repo.get_issues(state='open', labels=['status: selected'])
            if issues.totalCount == 0:
                await asyncio.sleep(60)
                continue

            # Establish the MCP connection on each loop sweep so it doesn't drop due to inactivity
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    mcp_tools = await load_mcp_tools(session)
                    
                    agent = create_react_agent(llm, local_tools + mcp_tools)
                    state = {
                        "messages": [
                            SystemMessage(content=prompt),
                            HumanMessage(content="Evaluate your assigned task queues and execute your workflow rules. Use GitHub MCP tools for remote git operations.")
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
    # Implementer requires highest reasoning to code features accurately
    multiprocessing.Process(target=create_daemon, args=("Implementer", IMPLEMENTER_PROMPT, "gemini-2.5-pro", 0.3)).start()