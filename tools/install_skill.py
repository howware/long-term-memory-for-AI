"""Install the project-memory skill so any new project can start a memory.

The skill is self-contained: SKILL.md plus the helper (memory.py and its
memkit package) and its migrations under scripts/. Installing never touches an
existing memory database.

    python tools/install_skill.py --target ~/.claude/skills/project-memory
    python tools/install_skill.py --target ~/.codex/skills/project-memory
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skill" / "project-memory" / "SKILL.md"
HELPER_SOURCE = ROOT / "db" / "memory.py"
PACKAGE_SOURCE = ROOT / "db" / "memkit"
MIGRATIONS_SOURCE = ROOT / "db" / "migrations"


def install(target: Path) -> list[Path]:
    scripts = target / "scripts"
    migrations = scripts / "migrations"
    package = scripts / "memkit"
    migrations.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    # Stale migrations would be applied to new memories and stale modules
    # could be imported, so both folders mirror the source exactly.
    for folder, source, pattern in ((migrations, MIGRATIONS_SOURCE, "*.sql"), (package, PACKAGE_SOURCE, "*.py")):
        for stale in folder.glob(pattern):
            if not (source / stale.name).exists():
                stale.unlink()
    copied = [
        Path(shutil.copy2(SKILL_SOURCE, target / "SKILL.md")),
        Path(shutil.copy2(HELPER_SOURCE, scripts / "memory.py")),
    ]
    copied += [Path(shutil.copy2(path, package / path.name)) for path in sorted(PACKAGE_SOURCE.glob("*.py"))]
    copied += [Path(shutil.copy2(path, migrations / path.name)) for path in sorted(MIGRATIONS_SOURCE.glob("*.sql"))]
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="Skill directory to create or update")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    for path in install(target):
        print(path)


if __name__ == "__main__":
    main()
