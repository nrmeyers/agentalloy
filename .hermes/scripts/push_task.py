#!/usr/bin/env python3
"""
Push a task file to the remote repo and create a PR.

Usage: python push_task.py <task_file>

This script:
1. Adds the task file to git
2. Creates a new branch from task-0-N (or task-N-N for N>0)
3. Commits the task
4. Pushes to origin
5. Creates a PR to main

The PR title follows the pattern: "Task N: <title>"
The PR description includes the task file content.
"""

import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a shell command and return output."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip()


def main():
    if len(sys.argv) < 2:
        print("Usage: python push_task.py <task_file>")
        sys.exit(1)

    task_file = Path(sys.argv[1]).resolve()
    if not task_file.exists():
        print(f"Task file not found: {task_file}")
        sys.exit(1)

    repo_root = task_file.parent.parent
    task_name = task_file.stem  # e.g., "task-0-extract-wire-legacy"

    print(f"Pushing task: {task_name}")
    print(f"Task file: {task_file}")

    # Parse task number from filename
    parts = task_name.split("-")
    task_num = 0 if len(parts) >= 2 and parts[1] == "0" else int(parts[1])

    # Create branch from task-0-N pattern
    branch_name = "task-0-prereq-refactors" if task_num == 0 else f"task-{task_num}-{task_num:02d}"

    # Check if branch already exists
    branch_exists = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=repo_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    if branch_exists:
        checkout = f"git checkout {branch_name}"
        print(f"Checked out existing branch: {branch_name}")
    else:
        checkout = f"git checkout -b {branch_name}"
        print(f"Created new branch: {branch_name}")

    # Add and commit task
    add_cmd = f"git add {task_file}"
    commit_msg = f"Task {task_num}: {task_file.stem.replace('task-', '')}"

    print(f"Step 1: {checkout}")
    run_cmd(checkout.split(), cwd=repo_root)

    print(f"Step 2: {add_cmd}")
    run_cmd(add_cmd.split(), cwd=repo_root)

    print(f"Step 3: git commit -m '{commit_msg}'")
    run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_root)

    # Push branch
    print(f"Step 4: git push -u origin {branch_name}")
    run_cmd(["git", "push", "-u", "origin", branch_name], cwd=repo_root)

    # Create PR
    pr_title = f"Task {task_num}: {task_file.stem.replace('task-', '')}"
    pr_body = (
        f"This PR implements {task_file.stem.replace('task-', '')} as defined in the task file.\n\n"
    )
    pr_body += "<details><summary>Task Details</summary>\n\n```md\n"
    pr_body += task_file.read_text()
    pr_body += "```\n\n</details>"

    print(f"Step 5: Creating PR '{pr_title}' to main")
    pr_cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--base",
        "main",
        "--head",
        branch_name,
        "--draft",  # Start as draft for review
    ]
    run_cmd(pr_cmd)

    print(f"\n✅ Task pushed! Branch: {branch_name}")
    print(f"   PR: {pr_title} (draft) to main")


if __name__ == "__main__":
    main()
