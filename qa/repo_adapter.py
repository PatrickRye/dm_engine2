import os
from filelock import FileLock
from langchain_core.tools import tool

# Files the AI is allowed to edit locally. Everything else is blocked.
ALLOWED_LIVE_FILES = [
    "server/tools.py",
    "server/prompts.py",
    "server/dnd_rules_engine.py",
    "server/compendium_manager.py"
]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_live_patch_mode() -> bool:
    return os.path.exists(os.path.join(BASE_DIR, ".live_patch_mode"))

# --- LOCAL LIVE TOOLS ---

@tool
def local_read_file(filepath: str) -> str:
    """Reads a file from the local repository."""
    full_path = os.path.join(BASE_DIR, filepath)
    if not os.path.exists(full_path):
        return f"SYSTEM ERROR: File {filepath} does not exist."
    
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"SYSTEM ERROR: Could not read file: {e}"

@tool
def local_write_file(filepath: str, content: str) -> str:
    """Writes code directly to the local repository for live patching."""
    # 1. Sandbox Restriction
    normalized_path = filepath.replace("\\", "/")
    if normalized_path not in ALLOWED_LIVE_FILES:
        return f"SYSTEM ERROR: You do not have permission to live-patch '{filepath}'. It is off-limits."

    full_path = os.path.join(BASE_DIR, filepath)
    
    # 2. Concurrency Locking
    lock = FileLock(f"{full_path}.lock", timeout=10)
    try:
        with lock:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        return f"MECHANICAL TRUTH: Successfully patched {filepath}. The server will hot-reload automatically."
    except Exception as e:
        return f"SYSTEM ERROR: Failed to write to {filepath}: {e}"

# --- ADAPTER FACTORY ---

def get_codebase_tools() -> list:
    """Returns the correct set of LangChain tools based on the environment."""
    if is_live_patch_mode():
        print("[Adapter] Running in LOCAL LIVE PATCH mode.")
        return [local_read_file, local_write_file]
    else:
        print("[Adapter] Running in CLOUD GITHUB mode.")
        # Return the MCP wrappers or GitHub API tools here instead
        return []