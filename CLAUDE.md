# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CORAL is an orchestration system for autonomous coding agents. Core loop: **spawn agents in git worktrees → agents read CORAL.md instructions → make changes → `coral eval` grades commits → agents iterate → shared knowledge via notes/skills**.

Agents are Claude Code / Codex / OpenCode subprocesses. Shared state lives in `.coral/public/` and is symlinked into every worktree so agents see each other's work in real time.

## Commands

```bash
# Install
uv sync                    # Basic install
uv sync --extra dev        # With pytest, ruff, mypy
uv sync --all-extras       # Everything including UI

# Run tests
uv run pytest tests/ -v                      # All tests
uv run pytest tests/test_config.py -v        # Single file
uv run pytest tests/test_config.py::test_name -v  # Single test

# Lint & format
uv run ruff check .        # Lint (rules: E, F, I, N, W, UP; E501 ignored)
uv run ruff format .       # Format
uv run ruff check --fix .  # Auto-fix lint issues

# Type check
uv run mypy coral/

# Run CORAL
uv run coral start -c task.yaml                    # Launch agents
uv run coral start -c task.yaml agents.count=4     # With dotlist overrides
uv run coral stop                                  # Stop all agents
uv run coral status                                # Agent health + leaderboard
uv run coral log                                   # Top 20 attempts
uv run coral ui                                    # Web dashboard (port 8420)
```

## Architecture

```text
coral/
├── types.py             # Task, Score, ScoreBundle, Attempt dataclasses (all have to_dict/from_dict)
├── config.py            # CoralConfig — OmegaConf structured merge with YAML + dotlist overrides
├── agent/
│   ├── manager.py       # AgentManager: lifecycle, monitoring loop, heartbeat dispatch, session persistence
│   ├── heartbeat.py     # HeartbeatRunner: interval/plateau triggers, cooldown tracking
│   └── builtin/         # Runtime implementations (claude_code.py, codex.py, opencode.py, kiro.py)
├── workspace/
│   ├── project.py       # create_project(): builds results/<task>/<timestamp>/ layout, clones repo
│   ├── worktree.py      # create_agent_worktree(): git worktree per agent, gitignore, symlinks
│   └── settings.py      # Per-runtime permission/settings writers (claude, codex, opencode)
├── grader/
│   ├── protocol.py      # GraderInterface protocol (@runtime_checkable)
│   ├── base.py          # BaseGrader ABC with _make_score(), _make_bundle() helpers
│   ├── task_grader.py   # TaskGrader: the typical base class — run_program(), fail(), score()
│   └── loader.py        # Dynamic import from eval/grader.py, looks for class Grader(TaskGrader)
├── hub/
│   ├── attempts.py      # Attempt JSON CRUD in .coral/public/attempts/<commit_hash>.json
│   ├── notes.py         # Markdown notes with YAML frontmatter in .coral/public/notes/
│   ├── skills.py        # Skill directories with SKILL.md in .coral/public/skills/
│   ├── checkpoint.py    # Git-based shared state versioning inside .coral/
│   └── heartbeat.py     # Heartbeat config CRUD (per-agent + global JSON files)
├── hooks/
│   └── post_commit.py   # run_eval(): git add+commit → grade in subprocess → write Attempt JSON
├── template/
│   └── coral_md.py      # Per-agent CORAL.md generation with conditional sections
├── web/                 # Starlette backend + React dashboard
└── cli/                 # argparse with grouped help, lazy imports, "did you mean?" suggestions
```

## Key Design Patterns

**Config system**: `CoralConfig` uses dataclasses + OmegaConf structured merge. YAML is loaded, preprocessed (legacy key normalization), then merged with OmegaConf for type-safe defaults. CLI dotlist overrides (`agents.count=4`) merge via `OmegaConf.from_dotlist()`. The `_preprocess()` step handles backward compatibility (old heartbeat keys → modern heartbeat action list).

**Runtime abstraction**: `AgentRuntime` is a Protocol with `start()` → `AgentHandle`. Each runtime (Claude Code, Codex, OpenCode, Kiro) implements runtime-specific CLI flags, instruction filenames (`CLAUDE.md` vs `AGENTS.md`), and shared directory names (`.claude` vs `.codex`). `AgentHandle` wraps `subprocess.Popen` with SIGTERM→SIGKILL escalation and session ID extraction from logs.

**Process isolation**: Each agent gets its own git worktree branch and its own Python venv via `UV_PROJECT_ENVIRONMENT` env var. This prevents concurrent `uv` operations from corrupting a shared venv.

**Grader execution**: Graders run in a child process (multiprocessing, not asyncio) so blocking code can be hard-killed on timeout. The `TaskGrader` base class provides `run_program(filename)` which executes files from the agent's codebase as a subprocess and returns `CompletedProcess`.

**Shared state via symlinks**: `.coral/public/{attempts,notes,skills,heartbeat}` is symlinked into each worktree. Agents read/write shared state without explicit coordination. Relative symlinks for cross-machine portability.

**Breadcrumb files**: `.coral_agent_id` and `.coral_dir` are written into each worktree so hooks can discover which agent and shared state directory to use without environment variables.

**Heartbeat system**: Two trigger types — `interval` (fires every N evals, modulo check) and `plateau` (fires when agent stalls N evals without score improvement, with cooldown). Actions can be per-agent (local) or global (across all agents). Prompts loaded from `coral/hub/prompts/` markdown files.

**Session persistence**: Claude Code session IDs are extracted from agent logs (JSON scanning in reverse) and saved to `sessions.json`. On resume, sessions are validated locally — different machines start fresh.

## Project Layout at Runtime

```text
results/<task-slug>/<timestamp>/
├── .coral/
│   ├── public/          # Shared state (symlinked into worktrees)
│   │   ├── attempts/    # Attempt JSONs
│   │   ├── notes/       # Agent notes (markdown + YAML frontmatter)
│   │   ├── skills/      # Skill directories with SKILL.md
│   │   ├── heartbeat/   # Per-agent and global heartbeat configs
│   │   └── logs/        # Agent logs
│   ├── private/         # Hidden from agents (eval/, grader, answer keys)
│   ├── config.yaml      # Serialized CoralConfig
│   └── config_dir       # Path to task directory for relative path resolution
├── repo/                # Cloned source repo
└── agents/
    └── agent-1/         # Git worktree with symlinks to .coral/public/
```

## Code Style

- Python 3.11+, line length 100
- Ruff for linting and formatting (target: py311)
- pytest with `asyncio_mode = "auto"` (no need for `@pytest.mark.asyncio`)
- Type annotations with `mypy --strict` (but `ignore_missing_imports = true`)
