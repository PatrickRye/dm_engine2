import os
import time
import shutil
import json
import subprocess
import multiprocessing

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

# --- 1. Directories & State Setup ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = os.path.join(BASE_DIR, "tasks")
INBOX_DIR = os.path.join(TASKS_DIR, "inbox")
BACKLOG_DIR = os.path.join(TASKS_DIR, "backlog")
SELECTED_DIR = os.path.join(TASKS_DIR, "selected")
ARCHIVED_DIR = os.path.join(TASKS_DIR, "archived")
LOCKS_FILE = os.path.join(TASKS_DIR, "active_locks.json")

for d in [INBOX_DIR, BACKLOG_DIR, SELECTED_DIR, ARCHIVED_DIR]:
    os.makedirs(d, exist_ok=True)

if not os.path.exists(LOCKS_FILE):
    with open(LOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

# --- 2. Shared Agent Tools ---
@tool
def list_tasks(status_directory: str) -> list:
    """Lists all .md files in 'inbox', 'backlog', 'selected', or 'archived'."""
    target_dir = os.path.join(TASKS_DIR, status_directory)
    if not os.path.exists(target_dir):
        return []
    return [os.path.join(target_dir, f) for f in os.listdir(target_dir) if f.endswith(".md")]

@tool
def read_file(filepath: str) -> str:
    """Reads the contents of any file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

@tool
def write_file(filepath: str, content: str) -> str:
    """Overwrites a file with new content."""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully wrote to {filepath}"
    except Exception as e:
        return f"Error writing file: {e}"

@tool
def move_task(filepath: str, destination_dir: str) -> str:
    """Moves a task markdown file to 'backlog', 'selected', or 'archived'."""
    try:
        dest = os.path.join(TASKS_DIR, destination_dir, os.path.basename(filepath))
        shutil.move(filepath, dest)
        return f"Moved task to {dest}"
    except Exception as e:
        return f"Error moving task: {e}"

@tool
def manage_locks(action: str, task_id: str = "", files: list[str] = None) -> str:
    """
    Manages concurrency file locks.
    - action='read': Returns the current JSON dict of locked files.
    - action='add': Locks the listed 'files' under 'task_id'.
    - action='remove': Releases all file locks held by 'task_id'.
    """
    try:
        with open(LOCKS_FILE, "r", encoding="utf-8") as f:
            locks = json.load(f)
        
        if action == "read":
            return json.dumps(locks, indent=2)
            
        if action == "add" and files:
            locks[task_id] = files
            with open(LOCKS_FILE, "w", encoding="utf-8") as f:
                json.dump(locks, f, indent=2)
            return f"Locked {len(files)} files for {task_id}."
            
        if action == "remove" and task_id in locks:
            del locks[task_id]
            with open(LOCKS_FILE, "w", encoding="utf-8") as f:
                json.dump(locks, f, indent=2)
            return f"Released locks for {task_id}."
            
        return "No changes made. Check parameters."
    except Exception as e:
        return f"Error managing locks: {e}"

@tool
def execute_shell_command(command: str) -> str:
    """
    Executes a shell command from the project's root directory.
    Use this to run git commands (git branch, git checkout, git commit) and pytest.
    """
    try:
        result = subprocess.run(command, shell=True, cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
        return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\nRETURN CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return "Command timed out after 120 seconds."
    except Exception as e:
        return f"Command failed to execute: {e}"

# --- 3. System Prompts ---
TRIAGER_PROMPT = """
Role: You are the Triager Agent for the D&D AI system. Your job is to evaluate newly reported bugs/issues, assign priority, categorize them, and move them to the backlog.

Allowed Actions:
1. Read `.md` files in `inbox` using `list_tasks` and `read_file`.
2. Modify the file content using `write_file` to update its frontmatter.
3. Move the file from `inbox` to `backlog` using `move_task`.

Execution Rules:
- Analyze tasks in the `inbox`.
- Assess priority (critical, high, medium, low) based on severity and the `frequency` counter in the frontmatter. Higher frequency = higher priority.
- Categorize the issue (`category`: narrative, rules, code, other) and assign a highly descriptive `sub_category`.
- Rewrite the YAML frontmatter with your updated fields and change `status: backlog`.
- Use `move_task` to send it to the `backlog` directory.
"""

PLANNER_PROMPT = """
Role: You are the Planner Agent for a Python Dungeons & Dragons engine. Your job is to schedule development tasks, manage Git branches, and prevent concurrent file modification conflicts.

Allowed Actions:
1. Read `.md` files in `backlog` using `list_tasks` and `read_file`.
2. Read and update `active_locks.json` using `manage_locks`.
3. Execute Git commands to create new branches from `main` using `execute_shell_command`.
4. Move files from `backlog` to `selected` using `move_task`, and update their metadata using `write_file`.

Execution Rules:
- Analyze tasks in `backlog` and schedule them in order of priority (critical first, then high, medium, low). Determine which Python files must be modified to fulfill it.
- Check `active_locks.json`. If the required files are currently locked by another active task, do not schedule this task. Select another.
- If the files are free:
  - Add the target files to active locks with the task ID.
  - Create a new Git branch named `feature/[TASK-ID]`.
  - Update the task Markdown file with the branch name and set `status: selected`.
  - Move the task to `selected`.
- If you encounter a task flagged with `status: merge_conflict`, assign it high priority, create a dedicated conflict-resolution branch, and move it to `selected` for immediate implementation.
"""

IMPLEMENTER_PROMPT = """
Role: You are the Implementer Agent. Your responsibility is to write Python code to fulfill predefined task requirements within specific Git branches.

Allowed Actions:
1. Read files in `selected` where `status: selected`.
2. Checkout the Git branch specified in the task file using `execute_shell_command`.
3. Read, modify, and create Python scripts to execute the task intent.
4. Commit changes, push to the remote repository, and mock a Merge Request (MR) targeting the `main` branch.
5. Update the task Markdown file to `status: needs_review`.

Execution Rules:
- You may only write code on the branch assigned to your current task. Do not checkout `main`.
- Analyze the task's `category` and `sub_category` from the frontmatter. Spawn/adapt your context accordingly:
  * If 'rules', focus heavily on `dnd_rules_engine.py`, `tools.py`, and game mechanics.
  * If 'narrative', focus heavily on `prompts.py` and state contexts.
  * If 'code', focus heavily on general infrastructure, API logic, or system stability.
- Ensure all new logic includes detailed inline logging using the system's JSON logging framework.
- Run local `pytest` checks before committing.
- When the code is complete, push your branch and update the task status to `needs_review`. Do not assign the next task until this process is fully complete.
"""

REVIEWER_PROMPT = """
Role: You are the Reviewer Agent. Your job is to audit branches, validate test results, and merge compliant code into the `main` branch.

Allowed Actions:
1. Read files in `selected` where `status: needs_review`.
2. Read diffs using `execute_shell_command` (e.g. `git diff main...feature/[TASK-ID]`).
3. Run the CI/CD test pipeline using `pytest test/server/`.
4. Execute Git merges or reject MRs.
5. Move task files to `archived` (if merged) or back to `backlog` (if rejected).
6. Remove cleared locks from `active_locks` using `manage_locks`.

Execution Rules:
- Verify that the code diff strictly aligns with the requirements listed in the original task Markdown file.
- Validate that the Merge Request (MR) description is highly accurate, formatted well, and suitable for Release Notes. If it is poor, reject it and demand better release notes.
- Verify that the automated test suite passed.
- If the test passes and the intent is met: Merge the branch into `main`, remove the files from active locks, and move the task to `archived`.
- If a merge conflict is detected upon attempting to merge, you MUST attempt to resolve the conflict yourself using `execute_shell_command` (e.g., `git merge`, edit file manually, `git commit`) before rejecting.
- If unresolvable or the test fails: Reject the merge, add a comment detailing the failure, set the task to `status: failed_review`, and move it back to `backlog`.
"""

# --- 4. Agent Loops ---
def create_daemon(name, prompt, model="gemini-2.5-pro", temp=0.1):
    print(f"[{name}] Daemon starting...")
    llm = ChatGoogleGenerativeAI(model=model, temperature=temp)
    tools = [list_tasks, read_file, write_file, move_task, manage_locks, execute_shell_command]
    agent = create_react_agent(llm, tools)
    
    while True:
        try:
            state = {
                "messages": [
                    SystemMessage(content=prompt),
                    HumanMessage(content="Evaluate your assigned task queues and execute your workflow rules.")
                ]
            }
            agent.invoke(state)
        except Exception as e:
            print(f"[{name}] Encountered error: {e}")
        
        time.sleep(60)  # Sweep directories every 60 seconds

if __name__ == "__main__":
    # NOTE: Run this daemon independently from FastAPI to avoid git checkout crashing the server
    multiprocessing.Process(target=create_daemon, args=("Triager", TRIAGER_PROMPT, "gemini-2.5-flash", 0.2)).start()
    multiprocessing.Process(target=create_daemon, args=("Planner", PLANNER_PROMPT, "gemini-2.5-flash", 0.1)).start()
    # Implementer requires highest reasoning to code features accurately
    multiprocessing.Process(target=create_daemon, args=("Implementer", IMPLEMENTER_PROMPT, "gemini-2.5-pro", 0.3)).start()
    # Reviewer requires strict strictness
    multiprocessing.Process(target=create_daemon, args=("Reviewer", REVIEWER_PROMPT, "gemini-2.5-pro", 0.0)).start()