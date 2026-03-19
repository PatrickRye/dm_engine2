import os
import shutil

def main():
    # Resolve the project root (one level up from the utils directory)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    
    directories_to_remove = ['.pytest_cache', 'htmlcov']
    files_to_remove = ['.coverage', 'flake8_report.txt']
    
    print(f"Cleaning caches in: {project_root}")
    
    deleted_dirs = 0
    deleted_files = 0
    
    # 1. Clean root-level cache directories and files
    for d in directories_to_remove:
        target = os.path.join(project_root, d)
        if os.path.exists(target) and os.path.isdir(target):
            try:
                shutil.rmtree(target)
                deleted_dirs += 1
            except Exception as e:
                print(f"Failed to remove {target}: {e}")
                
    for f in files_to_remove:
        target = os.path.join(project_root, f)
        if os.path.exists(target) and os.path.isfile(target):
            try:
                os.remove(target)
                deleted_files += 1
            except Exception as e:
                print(f"Failed to remove {target}: {e}")

    # 2. Recursively find and delete __pycache__ directories
    for root, dirs, files in os.walk(project_root):
        if '__pycache__' in dirs:
            target = os.path.join(root, '__pycache__')
            try:
                shutil.rmtree(target)
                deleted_dirs += 1
            except Exception as e:
                print(f"Failed to remove {target}: {e}")
            dirs.remove('__pycache__')  # Prevent os.walk from entering the deleted dir

    print(f"Cleanup complete. Successfully removed {deleted_dirs} cache directories and {deleted_files} files.")

if __name__ == "__main__":
    main()