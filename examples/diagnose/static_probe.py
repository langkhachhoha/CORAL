"""Static probe — bootstrap a fake CORAL worktree and dump its state.

Runs WITHOUT calling an LLM, so it works in CI without an API key. The
output mirrors what an agent would see when it lands in a real worktree:
the file tree, the breadcrumb files, the .claude/settings.json
permissions block, and what each symlink under .claude/ resolves to.

Usage:
  uv run python examples/diagnose/static_probe.py [output_dir]

The default output dir is ``./_probe`` (gitignored). If the directory
exists, it is reused so the probe is idempotent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    ).stdout


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "_probe").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    repo_dir = out_dir / "repo"
    coral_dir = out_dir / ".coral"
    public_dir = coral_dir / "public"
    private_dir = coral_dir / "private"
    agents_dir = out_dir / "agents"
    worktree = agents_dir / "agent-1"

    # Init a one-commit repo so `git worktree add` has something to branch from
    if not (repo_dir / ".git").exists():
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "probe.py").write_text("print('probe')\n")
        _run(["git", "init", "-q"], cwd=repo_dir)
        _run(["git", "-c", "user.email=p@p", "-c", "user.name=p", "add", "."], cwd=repo_dir)
        _run(
            ["git", "-c", "user.email=p@p", "-c", "user.name=p", "commit", "-qm", "init"],
            cwd=repo_dir,
        )

    for d in (public_dir / "notes", public_dir / "skills",
              public_dir / "attempts", public_dir / "logs",
              public_dir / "heartbeat", private_dir):
        d.mkdir(parents=True, exist_ok=True)
    (private_dir / "answer_key.txt").write_text("SECRET — agents must not read this\n")

    # Bootstrap the worktree using coral's own helpers
    from coral.workspace.worktree import (
        create_agent_worktree,
        setup_claude_settings,
        setup_gitignore,
        setup_shared_state,
        write_agent_id,
        write_coral_dir,
    )

    if not worktree.exists():
        create_agent_worktree(repo_dir, "agent-1", agents_dir)
    setup_gitignore(worktree)
    write_agent_id(worktree, "agent-1")
    write_coral_dir(worktree, coral_dir)
    setup_shared_state(worktree, coral_dir, shared_dir_name=".claude")
    setup_claude_settings(worktree, coral_dir, research=False)

    _section(f"WORKTREE TREE — {worktree}")
    print(_run(["find", ".", "-maxdepth", "4", "-not", "-path", "./.git*"], cwd=worktree))

    _section("BREADCRUMB FILES")
    for name in (".coral_agent_id", ".coral_dir"):
        p = worktree / name
        print(f"--- {name} ---")
        print(p.read_text() if p.exists() else "(missing)")

    _section(".claude/settings.json")
    settings = json.loads((worktree / ".claude" / "settings.json").read_text())
    print(json.dumps(settings, indent=2))

    _section(".claude/ SYMLINK TARGETS")
    claude_dir = worktree / ".claude"
    for entry in sorted(claude_dir.iterdir()):
        if entry.is_symlink():
            target = os.readlink(entry)
            resolved = (entry.parent / target).resolve()
            exists = resolved.exists()
            print(f"  {entry.name} -> {target}    (resolves: {resolved}  exists={exists})")
        else:
            print(f"  {entry.name}    (regular file/dir)")

    _section("WRITE PROBE — via .claude/notes/ symlink")
    note_path = claude_dir / "notes" / "static-probe.md"
    note_path.write_text("# static probe — write OK\n")
    print(f"wrote {note_path}")
    print(f"file exists at symlink path:   {note_path.exists()}")
    abs_path = public_dir / "notes" / "static-probe.md"
    print(f"file exists at absolute path:  {abs_path.exists()}")
    print(f"content via abs path: {abs_path.read_text().strip()!r}")

    _section("READ PROBE — files inside worktree")
    for rel in ("CORAL.md", "probe.py", ".gitignore"):
        p = worktree / rel
        print(f"  {rel}: exists={p.exists()}, size={p.stat().st_size if p.exists() else 0}")

    _section("DENY PROBE — .coral/private/")
    print(f"private dir resolved by allow rules: {private_dir}")
    print("private dir is in deny list above (Read).")
    print(f"private file exists on disk: {(private_dir / 'answer_key.txt').exists()}")

    print()
    print("STATIC PROBE COMPLETE.")
    print(f"Probe artifacts left in: {out_dir}")


if __name__ == "__main__":
    main()
