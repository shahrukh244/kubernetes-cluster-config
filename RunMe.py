#!/usr/bin/env python3
import os
import subprocess
import sys

# Path to svc.py directory
TARGET_DIR = "/root/kubernetes-cluster-config/kube-cluster-config/kube-svc-config"
SCRIPT = "svc.py"

def main():
    script_path = os.path.join(TARGET_DIR, SCRIPT)

    # Check if file exists
    if not os.path.isfile(script_path):
        print(f"Error: {script_path} not found!")
        sys.exit(1)

    # Change working directory
    os.chdir(TARGET_DIR)

    # Run the script
    try:
        subprocess.run(["python3", SCRIPT], check=True)
    except subprocess.CalledProcessError as e:
        print(f"svc.py exited with error: {e.returncode}")
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
