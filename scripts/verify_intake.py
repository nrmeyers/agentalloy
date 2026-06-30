"""Verify intake skills in the skill store and run reembed."""

import subprocess
import sys

from agentalloy.config import get_settings
from agentalloy.storage.open import open_skills


def main():
    settings = get_settings()
    store = open_skills(settings, read_only=True)
    try:
        rows = store.execute(
            "SELECT skill_id, skill_class, canonical_name FROM skills "
            "WHERE skill_id LIKE '%intake%'"
        )
        if not rows:
            print("ERROR: No intake skills found in the skill store")
            return 1
        print(f"Found {len(rows)} intake skill(s) in the skill store:")
        for r in rows:
            skill_id, skill_class, name = r
            print(f"  {skill_id} | class={skill_class} | {name}")
            if skill_class != "workflow":
                print(f"  WARNING: expected skill_class='workflow', got '{skill_class}'")
        print("\nNow running reembed...")
        result = subprocess.run(
            [sys.executable, "-m", "agentalloy.install", "reembed"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        return result.returncode
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
