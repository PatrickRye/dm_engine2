import os
import glob

def main():
    # Resolve the project root (one level up from the utils directory)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    
    logs_dir = os.path.join(project_root, "logs")
    
    if not os.path.exists(logs_dir):
        print(f"No logs directory found at {logs_dir}. Nothing to clean.")
        return

    # Search for all .jsonl files recursively in the logs directory
    search_pattern = os.path.join(logs_dir, "**", "*.jsonl")
    jsonl_files = glob.glob(search_pattern, recursive=True)
    
    if not jsonl_files:
        print("No .jsonl log files found to delete.")
        return
        
    deleted_count = 0
    for file_path in jsonl_files:
        try:
            os.remove(file_path)
            deleted_count += 1
        except Exception as e:
            print(f"Failed to delete {file_path}: {e}")
            
    print(f"Cleanup complete. Successfully permanently deleted {deleted_count} log file(s).")

if __name__ == "__main__":
    main()