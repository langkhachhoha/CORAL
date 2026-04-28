"""Trivial grader for the diagnose probe.

The probe task is about inspecting the filesystem, not solving a problem.
We score 1.0 if the agent successfully wrote both probe notes (via the
.claude/notes symlink AND via the absolute .coral/public/notes path),
0.0 otherwise. The feedback string captures what we actually saw on disk
so the eval log shows whether write access works on the runner.
"""

from __future__ import annotations

import os
from pathlib import Path

from coral.grader import TaskGrader
from coral.types import ScoreBundle


class Grader(TaskGrader):
    def evaluate(self) -> float | ScoreBundle:
        cwd = Path(self.codebase_path)
        notes_dir = cwd / ".claude" / "notes"
        wrote_via_symlink = (notes_dir / "diagnose-probe.md").exists()

        coral_dir_file = cwd / ".coral_dir"
        coral_dir = (
            Path(coral_dir_file.read_text().strip())
            if coral_dir_file.exists()
            else None
        )
        wrote_via_abs = (
            coral_dir is not None
            and (coral_dir / "public" / "notes" / "diagnose-probe-abs.md").exists()
        )

        feedback_lines = [
            f"cwd={cwd}",
            f".coral_dir={coral_dir}",
            f"wrote_via_symlink={wrote_via_symlink}",
            f"wrote_via_abs={wrote_via_abs}",
            f"notes_dir_exists={notes_dir.exists()}",
            f"notes_is_symlink={notes_dir.is_symlink()}",
        ]
        if notes_dir.is_symlink():
            feedback_lines.append(f"notes_symlink_target={os.readlink(notes_dir)}")

        score_value = 1.0 if (wrote_via_symlink and wrote_via_abs) else 0.0
        return self.score(
            score_value,
            explanation="probe write check",
            feedback="\n".join(feedback_lines),
        )
