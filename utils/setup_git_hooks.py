import os
import stat
import sys

def main():
    # Resolve project root
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    
    git_hooks_dir = os.path.join(project_root, ".git", "hooks")
    if not os.path.exists(git_hooks_dir):
        print(f"Error: .git/hooks directory not found at {git_hooks_dir}. Are you in a git repository?")
        return

    pre_commit_path = os.path.join(git_hooks_dir, "pre-commit")
    
    hook_script = """#!/bin/sh
# Pre-commit hook to clean up caches before committing

echo "[Git Hook] Running cache cleanup..."
python utils/cleanup_caches.py
"""

    try:
        with open(pre_commit_path, "w", newline='\n') as f:
            f.write(hook_script)
            
        # Make the script executable (crucial for Unix-like systems)
        if sys.platform != "win32":
            st = os.stat(pre_commit_path)
            os.chmod(pre_commit_path, st.st_mode | stat.S_IEXEC)
            
        print(f"Successfully installed pre-commit hook at {pre_commit_path}")
    except Exception as e:
        print(f"Failed to install pre-commit hook: {e}")

if __name__ == "__main__":
    main()