# pypath.py
import os
import sys
import subprocess

def add_to_path(path):
    # Get current PATH
    current_path = os.environ.get("PATH", "")
    if path in current_path:
        print(f"{path} is already in PATH.")
        return

    # Use setx to permanently add to PATH
    try:
        subprocess.run(["setx", "PATH", f"{current_path};{path}"], shell=True)
        print(f"Successfully added {path} to PATH.")
    except Exception as e:
        print(f"Error adding to PATH: {e}")

def main():
    # Get Python installation directory
    python_dir = os.path.dirname(sys.executable)
    scripts_dir = os.path.join(python_dir, "Scripts")

    print(f"Python directory: {python_dir}")
    print(f"Scripts directory: {scripts_dir}")

    # Add both to PATH
    add_to_path(python_dir)
    add_to_path(scripts_dir)

if __name__ == "__main__":
    main()
