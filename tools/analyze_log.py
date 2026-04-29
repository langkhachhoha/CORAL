#!/usr/bin/env python3
"""Analyze a CORAL agent log.txt and emit a visual dashboard.

Usage:
    python tools/analyze_log.py log.txt
    python tools/analyze_log.py log.txt -o analysis/

The script auto-detects the benchmark name from the log and writes a folder
named after it (under the chosen output root) with:

  report.html     interactive dashboard (Plotly via CDN, open in any browser)
  summary.txt     plain-text run summary
  evals.csv       eval # / score / rank / timestamp / gap-to-best
  sessions.csv    per-API-session token + cost breakdown
  timeline.csv    cumulative tokens & cost over time
  buckets.csv     5/10/15-min aggregations
  tools.csv       tool-call frequency
  activity.txt    chronological human-readable feed of key events

Only stdlib is used.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# GitHub Actions wraps every line with `YYYY-MM-DDTHH:MM:SS.fractionalZ `.
# When present, it's the most reliable timestamp source — every event line carries
# one. We strip it first and feed the rest into the existing matchers.
GHA_PREFIX_RE = re.compile(
    r"^(?P<gha_ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) ?(?P<rest>.*)$"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LINE_AGENT_RE = re.compile(r"^\[(?P<prefix>agent-\d+|coral)\] (?P<rest>.+)$")
LINE_FRAMEWORK_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2}) \[(?P<module>[^\]]+)\] (?P<level>[A-Z]+): (?P<msg>.*)$"
)
EVAL_SCORE_RE = re.compile(r"Agent eval #(?P<n>\d+):\s*score=(?P<score>[^\s]+)")
HEARTBEAT_RE = re.compile(r"\(agent eval #(?P<n>\d+)\):\s*interrupting")
CWD_TIMESTAMP_RE = re.compile(r"results/(?P<bench>[^/]+)/(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6})")


@dataclass
class Session:
    """One API segment (= one `"type":"result"` event in the Claude Code stream).

    NOT the same as a Claude Code session: a single ``session_id`` can produce many
    result events when the streaming is interrupted (heartbeat, auth retry,
    aborted_streaming, etc.). ``ParsedLog.session_ids`` holds the distinct
    Claude Code session IDs observed across all segments.
    """

    index: int
    end_time: datetime | None
    duration_ms: int
    duration_api_ms: int
    num_turns: int
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    cost_usd: float
    is_error: bool
    stop_reason: str
    terminal_reason: str
    session_id: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_creation


@dataclass
class Eval:
    index: int
    score: float | None
    timestamp: datetime | None


@dataclass
class ToolCall:
    name: str
    summary: str
    timestamp: datetime | None


@dataclass
class Activity:
    timestamp: datetime | None
    kind: str  # "tool", "text", "thinking", "eval", "heartbeat", "system"
    detail: str


@dataclass
class ParsedLog:
    benchmark: str = "unknown-benchmark"
    run_date: str | None = None
    run_started: datetime | None = None
    run_ended: datetime | None = None
    sessions: list[Session] = field(default_factory=list)
    evals: list[Eval] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(s.cost_usd for s in self.sessions)

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.sessions)

    @property
    def total_input(self) -> int:
        return sum(s.input_tokens for s in self.sessions)

    @property
    def total_output(self) -> int:
        return sum(s.output_tokens for s in self.sessions)

    @property
    def total_cache_read(self) -> int:
        return sum(s.cache_read for s in self.sessions)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _frame_time(time_str: str, run_date: str | None) -> datetime | None:
    """Combine a HH:MM:SS framework log time with the run start date."""
    if not run_date:
        return None
    try:
        return datetime.fromisoformat(f"{run_date}T{time_str}+00:00")
    except ValueError:
        return None


def _summarize_tool_input(name: str, tool_input: dict[str, Any]) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        return _truncate(cmd.replace("\n", " "), 140)
    for key in ("file_path", "path", "notebook_path"):
        if key in tool_input:
            return _truncate(str(tool_input[key]), 140)
    if name == "Edit":
        return _truncate(str(tool_input.get("old_string", ""))[:60], 140)
    if name == "WebFetch":
        return _truncate(str(tool_input.get("url", "")), 140)
    if name == "WebSearch":
        return _truncate(str(tool_input.get("query", "")), 140)
    if name == "TodoWrite":
        todos = tool_input.get("todos", [])
        return f"{len(todos)} todos"
    if "description" in tool_input:
        return _truncate(str(tool_input["description"]), 140)
    return _truncate(json.dumps(tool_input)[:200], 200)


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_log(path: Path) -> ParsedLog:
    parsed = ParsedLog()
    last_event_time: datetime | None = None
    session_idx = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line:
                continue

            # GitHub Actions raw logs prefix every line with `YYYY-MM-DDTHH:MM:SS.fractionalZ `.
            # When present, that timestamp wins over anything embedded inside the JSON
            # (and gives us a real timestamp on every event, not just user events).
            gha_ts: datetime | None = None
            gha_match = GHA_PREFIX_RE.match(line)
            if gha_match:
                gha_ts = _parse_iso(gha_match.group("gha_ts"))
                line = gha_match.group("rest")
                if gha_ts:
                    last_event_time = gha_ts
                    if not parsed.run_started:
                        parsed.run_started = gha_ts
                    parsed.run_ended = gha_ts

            # Strip ANSI color escapes (with or without the leading ESC byte).
            line = ANSI_RE.sub("", line)
            if not line:
                continue

            agent_match = LINE_AGENT_RE.match(line)
            if agent_match:
                prefix = agent_match.group("prefix")
                rest = agent_match.group("rest")

                if prefix == "coral":
                    m = EVAL_SCORE_RE.search(rest)
                    if m:
                        idx = int(m.group("n"))
                        raw_score = m.group("score")
                        score = None if raw_score in ("None", "nan", "null") else float(raw_score)
                        parsed.evals.append(Eval(index=idx, score=score, timestamp=last_event_time))
                        parsed.activity.append(
                            Activity(
                                timestamp=last_event_time,
                                kind="eval",
                                detail=(
                                    f"Eval #{idx}: "
                                    + ("DNF / no score" if score is None else f"score={score:.6f}")
                                ),
                            )
                        )
                    continue

                # agent-N JSON
                try:
                    obj = json.loads(rest)
                except json.JSONDecodeError:
                    continue

                t = obj.get("type")
                # If we don't have a GHA prefix on this line (older log format), fall
                # back to the embedded user-event timestamp.
                if gha_ts is None:
                    ts = _parse_iso(obj.get("timestamp"))
                    if ts:
                        last_event_time = ts
                        if not parsed.run_started:
                            parsed.run_started = ts
                        parsed.run_ended = ts

                if t == "system" and obj.get("subtype") == "init":
                    cwd = obj.get("cwd", "") or ""
                    cwd_match = CWD_TIMESTAMP_RE.search(cwd)
                    if cwd_match and parsed.benchmark == "unknown-benchmark":
                        parsed.benchmark = cwd_match.group("bench")
                        stamp = cwd_match.group("stamp")
                        parsed.run_date = stamp.split("_")[0]

                elif t == "assistant":
                    content = (obj.get("message") or {}).get("content") or []
                    for block in content:
                        btype = block.get("type")
                        if btype == "tool_use":
                            name = block.get("name", "?")
                            summary = _summarize_tool_input(name, block.get("input") or {})
                            parsed.tool_calls.append(
                                ToolCall(name=name, summary=summary, timestamp=last_event_time)
                            )
                            parsed.activity.append(
                                Activity(
                                    timestamp=last_event_time,
                                    kind="tool",
                                    detail=f"{name}: {summary}" if summary else name,
                                )
                            )
                        elif btype == "text":
                            text = block.get("text", "").strip()
                            if text:
                                parsed.activity.append(
                                    Activity(
                                        timestamp=last_event_time,
                                        kind="text",
                                        detail=_truncate(text.replace("\n", " "), 220),
                                    )
                                )
                        elif btype == "thinking":
                            text = block.get("thinking", "").strip()
                            if text:
                                parsed.activity.append(
                                    Activity(
                                        timestamp=last_event_time,
                                        kind="thinking",
                                        detail=_truncate(text.replace("\n", " "), 220),
                                    )
                                )

                elif t == "result":
                    session_idx += 1
                    # Claude Code emits two parallel token counters:
                    #   usage.*           — canonical, but EMPTY (zero) for ~20-30%
                    #                        of result events when streaming was
                    #                        interrupted/aborted before usage was
                    #                        finalized.
                    #   modelUsage[model] — per-model breakdown, populated from the
                    #                        OpenRouter response. Authoritative.
                    # We sum modelUsage values if present and fall back to usage.*.
                    usage = obj.get("usage") or {}
                    mu = obj.get("modelUsage") or {}
                    in_tok = sum(int(m.get("inputTokens") or 0) for m in mu.values())
                    out_tok = sum(int(m.get("outputTokens") or 0) for m in mu.values())
                    cr_tok = sum(int(m.get("cacheReadInputTokens") or 0) for m in mu.values())
                    cc_tok = sum(int(m.get("cacheCreationInputTokens") or 0) for m in mu.values())
                    if not mu:
                        in_tok = int(usage.get("input_tokens") or 0)
                        out_tok = int(usage.get("output_tokens") or 0)
                        cr_tok = int(usage.get("cache_read_input_tokens") or 0)
                        cc_tok = int(usage.get("cache_creation_input_tokens") or 0)
                    parsed.sessions.append(
                        Session(
                            index=session_idx,
                            end_time=last_event_time,
                            duration_ms=int(obj.get("duration_ms") or 0),
                            duration_api_ms=int(obj.get("duration_api_ms") or 0),
                            num_turns=int(obj.get("num_turns") or 0),
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            cache_read=cr_tok,
                            cache_creation=cc_tok,
                            cost_usd=float(obj.get("total_cost_usd") or 0.0),
                            is_error=bool(obj.get("is_error")),
                            stop_reason=str(obj.get("stop_reason") or ""),
                            terminal_reason=str(obj.get("terminal_reason") or ""),
                            session_id=str(obj.get("session_id") or ""),
                        )
                    )
                continue

            fw_match = LINE_FRAMEWORK_RE.match(line)
            if fw_match:
                # Only fall back to the HH:MM:SS framework time if we don't have a
                # GHA timestamp (older log format) — GHA prefix is more precise.
                if gha_ts is None:
                    t = _frame_time(fw_match.group("time"), parsed.run_date)
                    if t:
                        last_event_time = t
                        if not parsed.run_started:
                            parsed.run_started = t
                        parsed.run_ended = t
                msg = fw_match.group("msg")
                hb = HEARTBEAT_RE.search(msg)
                if hb:
                    parsed.activity.append(
                        Activity(
                            timestamp=last_event_time,
                            kind="heartbeat",
                            detail=f"Heartbeat fired before eval #{hb.group('n')}",
                        )
                    )

    # Backfill eval timestamps using nearby framework heartbeat timestamps when missing
    eval_by_idx = {e.index: e for e in parsed.evals}
    for act in parsed.activity:
        if act.kind == "heartbeat":
            m = re.search(r"eval #(\d+)", act.detail)
            if m:
                idx = int(m.group(1))
                ev = eval_by_idx.get(idx)
                if ev and ev.timestamp is None and act.timestamp:
                    ev.timestamp = act.timestamp

    # Backfill activity timestamps from the next known timestamp (for events that
    # arrived before the first user event carrying a real timestamp).
    next_ts: datetime | None = None
    for act in reversed(parsed.activity):
        if act.timestamp:
            next_ts = act.timestamp
        elif next_ts:
            act.timestamp = next_ts

    parsed.evals.sort(key=lambda e: e.index)
    return parsed


# -------- aggregation helpers --------


def make_buckets(parsed: ParsedLog, minutes: int) -> list[dict[str, Any]]:
    """Bucket sessions by time-since-start, return both per-bucket and cumulative.

    Each row covers minutes ``[bucket_start_min, bucket_end_min)``. The full range
    from 0 up to the run end is filled — empty buckets get 0s — so the cumulative
    series advances smoothly through quiet periods.
    """
    if not parsed.run_started or not parsed.sessions:
        return []
    start = parsed.run_started
    end_t = parsed.run_ended or start
    total_min = max((end_t - start).total_seconds() / 60.0, 0)
    n_buckets = max(int(total_min // minutes) + 1, 1)

    bucket: dict[int, dict[str, Any]] = {
        i: {"tokens": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0, "sessions": 0}
        for i in range(n_buckets)
    }
    for s in parsed.sessions:
        if not s.end_time:
            continue
        delta = (s.end_time - start).total_seconds() / 60.0
        idx = max(int(delta // minutes), 0)
        if idx >= n_buckets:
            idx = n_buckets - 1
        b = bucket[idx]
        b["tokens"] += s.total_tokens
        b["input"] += s.input_tokens
        b["output"] += s.output_tokens
        b["cache_read"] += s.cache_read
        b["cost"] += s.cost_usd
        b["sessions"] += 1

    rows = []
    cum_tokens = cum_input = cum_output = cum_cache = 0
    cum_cost = 0.0
    cum_sessions = 0
    for idx in range(n_buckets):
        b = bucket[idx]
        cum_tokens += b["tokens"]
        cum_input += b["input"]
        cum_output += b["output"]
        cum_cache += b["cache_read"]
        cum_cost += b["cost"]
        cum_sessions += b["sessions"]
        rows.append(
            {
                "bucket_start_min": idx * minutes,
                "bucket_end_min": (idx + 1) * minutes,
                **b,
                "tokens_cum": cum_tokens,
                "input_cum": cum_input,
                "output_cum": cum_output,
                "cache_read_cum": cum_cache,
                "cost_cum": round(cum_cost, 6),
                "sessions_cum": cum_sessions,
            }
        )
    return rows


def build_eval_table(parsed: ParsedLog) -> list[dict[str, Any]]:
    """Add ranking and gap-to-best to eval rows."""
    scored = [e for e in parsed.evals if e.score is not None]
    sorted_by_score = sorted(scored, key=lambda e: e.score, reverse=True)
    rank: dict[int, int] = {e.index: i + 1 for i, e in enumerate(sorted_by_score)}
    best = sorted_by_score[0].score if sorted_by_score else None
    rows = []
    for e in parsed.evals:
        rows.append(
            {
                "eval": e.index,
                "score": e.score,
                "rank": rank.get(e.index),
                "gap_to_best": (
                    None if (e.score is None or best is None) else round(best - e.score, 6)
                ),
                "timestamp": e.timestamp.isoformat() if e.timestamp else "",
            }
        )
    return rows


# -------- writers --------


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_summary(parsed: ParsedLog, evals: list[dict[str, Any]], out: Path) -> None:
    scored = [r for r in evals if r["score"] is not None]
    best = max(scored, key=lambda r: r["score"]) if scored else None
    duration_min = (
        ((parsed.run_ended - parsed.run_started).total_seconds() / 60.0)
        if (parsed.run_started and parsed.run_ended)
        else 0.0
    )
    tools = Counter(t.name for t in parsed.tool_calls)
    distinct_sessions = len({s.session_id for s in parsed.sessions if s.session_id})
    error_segments = sum(1 for s in parsed.sessions if s.is_error)
    # Implied per-token rate — useful for spotting when Claude Code SDK fell back
    # to default Anthropic-style pricing instead of the real provider rate.
    implied_in_rate = (
        parsed.total_cost / parsed.total_input * 1e6 if parsed.total_input else 0.0
    )
    lines = [
        f"Benchmark : {parsed.benchmark}",
        f"Run date  : {parsed.run_date or '?'}",
        f"Run start : {parsed.run_started.isoformat() if parsed.run_started else '?'}",
        f"Run end   : {parsed.run_ended.isoformat() if parsed.run_ended else '?'}",
        f"Duration  : {duration_min:.1f} min",
        "",
        f"Evals (coral eval submissions): {len(parsed.evals)}"
        f"  (scored={len(scored)}, DNF={len(parsed.evals) - len(scored)})",
        f"Best score: {best['score']:.6f} (eval #{best['eval']})" if best else "Best score: —",
        "",
        f"API segments (result events): {len(parsed.sessions)}"
        f"  (errored={error_segments})",
        f"Claude Code sessions        : {distinct_sessions}"
        f"  (distinct session_id values; one process can emit many segments)",
        "",
        f"Estimated cost (per CC SDK)  : ${parsed.total_cost:.4f}",
        f"  Implied input rate         : ${implied_in_rate:.2f}/M tokens",
        "  WARNING: Claude Code SDK computes total_cost_usd using its internal",
        "  pricing table. If you routed through OpenRouter / a non-Anthropic",
        "  provider, the SDK likely fell back to Anthropic-style rates ($5/M input)",
        "  and this number can be 5–20× higher than what your provider actually",
        "  charges. Check your provider dashboard (e.g. openrouter.ai/activity)",
        "  for the real billed amount.",
        "",
        f"Total tokens     : {parsed.total_tokens:,}",
        f"  input          : {parsed.total_input:,}",
        f"  output         : {parsed.total_output:,}",
        f"  cache read     : {parsed.total_cache_read:,}",
        "",
        "Top tools used:",
    ]
    for name, count in tools.most_common(10):
        lines.append(f"  {name:20s} {count:5d}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_activity(parsed: ParsedLog, out: Path) -> None:
    """Compact human-readable feed of key actions."""
    lines: list[str] = []
    for a in parsed.activity:
        ts = a.timestamp.strftime("%H:%M:%S") if a.timestamp else "  ??:??:??"
        kind_tag = {
            "eval": "[EVAL] ",
            "tool": "[TOOL] ",
            "text": "[SAY]  ",
            "thinking": "[THINK]",
            "heartbeat": "[BEAT] ",
            "system": "[SYS]  ",
        }.get(a.kind, "[--]   ")
        lines.append(f"{ts} {kind_tag} {a.detail}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -------- HTML dashboard --------


HTML_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  :root {{ --fg:#1e293b; --muted:#64748b; --bg:#f8fafc; --card:#ffffff;
          --accent:#2563eb; --good:#16a34a; --warn:#dc2626; }}
  body {{ font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
          color:var(--fg); background:var(--bg); margin:0; padding:24px; }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  h2 {{ font-size:16px; margin:24px 0 8px; }}
  .sub {{ color:var(--muted); margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:12px; margin-bottom:16px; }}
  .card {{ background:var(--card); border:1px solid #e2e8f0; border-radius:8px;
          padding:14px 16px; }}
  .stat-label {{ color:var(--muted); font-size:12px; text-transform:uppercase;
          letter-spacing:.04em; }}
  .stat-value {{ font-size:22px; font-weight:600; margin-top:4px; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .row > div {{ background:var(--card); border:1px solid #e2e8f0; border-radius:8px;
          padding:8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #e2e8f0; text-align:left; }}
  th {{ background:#f1f5f9; }}
  tr.best td {{ background:#dcfce7; font-weight:600; }}
  tr.dnf td {{ color:var(--warn); }}
  .activity {{ background:var(--card); border:1px solid #e2e8f0; border-radius:8px;
          padding:8px 0; max-height:480px; overflow:auto; font-family:ui-monospace,
          monospace; font-size:12px; }}
  .activity div {{ padding:2px 12px; border-bottom:1px solid #f1f5f9; white-space:nowrap;
          overflow:hidden; text-overflow:ellipsis; }}
  .tag {{ display:inline-block; min-width:54px; color:var(--muted); }}
  .tag-eval {{ color:var(--good); font-weight:600; }}
  .tag-tool {{ color:var(--accent); }}
  .tag-think {{ color:#9333ea; }}
  .tag-beat {{ color:#f59e0b; }}
  @media (max-width:900px) {{ .row {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="sub">{subtitle}</div>

<div style="background:#fef9c3; border:1px solid #facc15; border-radius:8px;
            padding:10px 14px; margin-bottom:16px; font-size:13px;">
  <strong>Cost is a Claude Code SDK estimate, not the real provider charge.</strong>
  The SDK applies its internal pricing table (typically Anthropic Sonnet-style:
  $5/M input + $25/M output) regardless of where the call was routed. If you used
  OpenRouter / a non-Anthropic provider, the actual billed amount can be 5–20×
  lower. Check your provider's dashboard (e.g. openrouter.ai/activity) for the
  ground truth, then plug your rates into the calculator below.
</div>

<div class="card" style="margin-bottom:16px;">
  <div class="stat-label">Cost calculator — enter your provider's real rates</div>
  <div style="display:flex; gap:16px; flex-wrap:wrap; align-items:center;
              margin-top:10px; font-size:14px;">
    <label>Input $/M
      <input id="rate-in" type="number" step="0.01" value="0.30"
             style="width:80px; margin-left:6px;">
    </label>
    <label>Output $/M
      <input id="rate-out" type="number" step="0.01" value="1.20"
             style="width:80px; margin-left:6px;">
    </label>
    <label>Cache read $/M
      <input id="rate-cache" type="number" step="0.01" value="0"
             style="width:80px; margin-left:6px;">
    </label>
    <div>
      <strong>Real cost: <span id="real-cost"
        style="color:var(--good); font-size:18px;">—</span></strong>
      <span id="real-cost-breakdown" style="color:var(--muted); margin-left:8px;"></span>
    </div>
  </div>
  <div style="font-size:12px; color:var(--muted); margin-top:8px;">
    <code>{tok_in:,}</code> input × rate + <code>{tok_out:,}</code> output × rate
    + <code>{tok_cache:,}</code> cache read × rate. Defaults are typical
    minimax/m2 OpenRouter pricing — adjust to your actual rates.
  </div>
</div>

<div class="grid">
  {stat_cards}
</div>

<h2>Eval scores &amp; ranking</h2>
<div class="row">
  <div id="chart-scores"></div>
  <div>
    <table>
      <thead><tr><th>Rank</th><th>Eval #</th><th>Score</th><th>Δ to best</th><th>Time</th></tr></thead>
      <tbody>{eval_rows}</tbody>
    </table>
  </div>
</div>

<h2>Token usage over time</h2>
<div class="row">
  <div id="chart-tokens-cum"></div>
  <div id="chart-cost-cum"></div>
</div>

<h2>Cumulative tokens &amp; cost every {bucket_label} min</h2>
<div class="row">
  <div id="chart-tokens-bucket"></div>
  <div id="chart-cost-bucket"></div>
</div>

<h2>Tool usage</h2>
<div class="row">
  <div id="chart-tools"></div>
  <div>
    <table>
      <thead><tr><th>Tool</th><th>Calls</th></tr></thead>
      <tbody>{tool_rows}</tbody>
    </table>
  </div>
</div>

<h2>API segments <span style="color:var(--muted); font-weight:normal; font-size:13px;">
    (one row per Claude Code <code>"type":"result"</code> event)</span></h2>
<div style="background:var(--card); border:1px solid #e2e8f0; border-radius:8px;
            padding:0; max-height:360px; overflow:auto;">
  <table>
    <thead><tr><th>#</th><th>End</th><th>Dur (s)</th><th>Turns</th>
        <th>Input</th><th>Output</th><th>Cache R</th><th>Cost</th><th>Status</th></tr></thead>
    <tbody>{session_rows}</tbody>
  </table>
</div>

<h2>Activity timeline</h2>
<div class="activity">{activity_rows}</div>

<script>
const TOK_IN = {tok_in};
const TOK_OUT = {tok_out};
const TOK_CACHE = {tok_cache};

function recalc() {{
  const ri = parseFloat(document.getElementById('rate-in').value)  || 0;
  const ro = parseFloat(document.getElementById('rate-out').value) || 0;
  const rc = parseFloat(document.getElementById('rate-cache').value)|| 0;
  const ci = TOK_IN  / 1e6 * ri;
  const co = TOK_OUT / 1e6 * ro;
  const cc = TOK_CACHE / 1e6 * rc;
  const total = ci + co + cc;
  document.getElementById('real-cost').textContent = '$' + total.toFixed(4);
  document.getElementById('real-cost-breakdown').textContent =
    '= $' + ci.toFixed(2) + ' in + $' + co.toFixed(2) + ' out + $' + cc.toFixed(2) + ' cache';
}}
document.getElementById('rate-in').addEventListener('input', recalc);
document.getElementById('rate-out').addEventListener('input', recalc);
document.getElementById('rate-cache').addEventListener('input', recalc);
recalc();

const SCORES_DATA = {scores_json};
const TOKENS_TS_DATA = {tokens_ts_json};
const COST_TS_DATA = {cost_ts_json};
const BUCKETS_DATA = {buckets_json};
const TOOLS_DATA = {tools_json};

const baseLayout = {{ margin:{{l:50,r:20,t:30,b:40}}, font:{{family:'ui-sans-serif'}},
                      paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)' }};

if (SCORES_DATA.x.length) {{
  Plotly.newPlot('chart-scores', [
    {{x: SCORES_DATA.x, y: SCORES_DATA.y, mode:'lines+markers', name:'Score',
      line:{{color:'#2563eb'}}, marker:{{size:8}}}},
    {{x: SCORES_DATA.best_x, y: SCORES_DATA.best_y, mode:'markers', name:'Best',
      marker:{{size:14,color:'#16a34a',symbol:'star'}}}}
  ], {{...baseLayout, title:'Score per eval', xaxis:{{title:'Eval #'}},
       yaxis:{{title:'Score'}}}}, {{displayModeBar:false}});
}}

if (TOKENS_TS_DATA.x.length) {{
  Plotly.newPlot('chart-tokens-cum', [
    {{x:TOKENS_TS_DATA.x, y:TOKENS_TS_DATA.input_cum, name:'input', stackgroup:'a',
      line:{{color:'#2563eb'}}}},
    {{x:TOKENS_TS_DATA.x, y:TOKENS_TS_DATA.output_cum, name:'output', stackgroup:'a',
      line:{{color:'#16a34a'}}}},
    {{x:TOKENS_TS_DATA.x, y:TOKENS_TS_DATA.cache_cum, name:'cache read', stackgroup:'a',
      line:{{color:'#9333ea'}}}}
  ], {{...baseLayout, title:'Cumulative tokens', xaxis:{{title:'time'}},
       yaxis:{{title:'tokens'}}}}, {{displayModeBar:false}});
}}

if (COST_TS_DATA.x.length) {{
  Plotly.newPlot('chart-cost-cum', [
    {{x:COST_TS_DATA.x, y:COST_TS_DATA.cum, mode:'lines+markers', fill:'tozeroy',
      line:{{color:'#dc2626'}}, name:'USD'}}
  ], {{...baseLayout, title:'Cumulative cost (USD)', xaxis:{{title:'time'}},
       yaxis:{{title:'$'}}}}, {{displayModeBar:false}});
}}

if (BUCKETS_DATA.labels.length) {{
  Plotly.newPlot('chart-tokens-bucket', [
    {{x:BUCKETS_DATA.labels, y:BUCKETS_DATA.input_cum, type:'bar', name:'input',
      marker:{{color:'#2563eb'}}}},
    {{x:BUCKETS_DATA.labels, y:BUCKETS_DATA.output_cum, type:'bar', name:'output',
      marker:{{color:'#16a34a'}}}},
    {{x:BUCKETS_DATA.labels, y:BUCKETS_DATA.cache_cum, type:'bar', name:'cache read',
      marker:{{color:'#9333ea'}}}}
  ], {{...baseLayout, title:'Cumulative tokens by minute (stacked)',
       barmode:'stack',
       xaxis:{{title:'≤ minutes from start'}}, yaxis:{{title:'tokens'}}}},
     {{displayModeBar:false}});
  Plotly.newPlot('chart-cost-bucket', [
    {{x:BUCKETS_DATA.labels, y:BUCKETS_DATA.cost_cum, type:'bar',
      marker:{{color:'#dc2626'}}, name:'SDK est. cost',
      text: BUCKETS_DATA.cost_cum.map(v => '$' + v.toFixed(2)),
      textposition:'outside', cliponaxis:false}}
  ], {{...baseLayout, title:'Cumulative SDK-estimated cost (USD)',
       xaxis:{{title:'≤ minutes from start'}}, yaxis:{{title:'$'}}}},
     {{displayModeBar:false}});
}}

if (TOOLS_DATA.labels.length) {{
  Plotly.newPlot('chart-tools', [
    {{x:TOOLS_DATA.values, y:TOOLS_DATA.labels, type:'bar', orientation:'h',
      marker:{{color:'#2563eb'}}}}
  ], {{...baseLayout, title:'Tool calls', xaxis:{{title:'count'}},
       margin:{{l:120,r:20,t:30,b:40}}}}, {{displayModeBar:false}});
}}
</script>
</body>
</html>
"""


def _stat_card(label: str, value: str) -> str:
    return (
        f'<div class="card"><div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{html.escape(value)}</div></div>'
    )


def _eval_rows_html(evals: list[dict[str, Any]]) -> str:
    if not evals:
        return '<tr><td colspan="5">No evals.</td></tr>'
    sorted_by_rank = sorted(
        evals, key=lambda r: (r["rank"] is None, r["rank"] if r["rank"] is not None else 0)
    )
    out = []
    best_rank = next((r["rank"] for r in sorted_by_rank if r["rank"] is not None), None)
    for r in sorted_by_rank:
        cls = ""
        if r["score"] is None:
            cls = "dnf"
        elif r["rank"] == best_rank:
            cls = "best"
        rank = "—" if r["rank"] is None else r["rank"]
        score = "DNF" if r["score"] is None else f"{r['score']:.6f}"
        gap = "—" if r["gap_to_best"] is None else f"{r['gap_to_best']:.6f}"
        ts = (r["timestamp"] or "")[11:19]
        out.append(
            f'<tr class="{cls}"><td>{rank}</td><td>{r["eval"]}</td>'
            f'<td>{score}</td><td>{gap}</td><td>{html.escape(ts)}</td></tr>'
        )
    return "\n".join(out)


def _session_rows_html(parsed: ParsedLog) -> str:
    out = []
    for s in parsed.sessions:
        end = s.end_time.strftime("%H:%M:%S") if s.end_time else ""
        status_cls = "dnf" if s.is_error else ""
        status = s.terminal_reason or s.stop_reason or ("ok" if not s.is_error else "error")
        out.append(
            f'<tr class="{status_cls}"><td>{s.index}</td><td>{end}</td>'
            f"<td>{s.duration_ms / 1000:.1f}</td><td>{s.num_turns}</td>"
            f"<td>{s.input_tokens:,}</td><td>{s.output_tokens:,}</td>"
            f"<td>{s.cache_read:,}</td><td>${s.cost_usd:.4f}</td>"
            f"<td>{html.escape(status)}</td></tr>"
        )
    return "\n".join(out)


def _activity_rows_html(parsed: ParsedLog, limit: int = 600) -> str:
    rows = []
    items = parsed.activity[-limit:] if len(parsed.activity) > limit else parsed.activity
    for a in items:
        ts = a.timestamp.strftime("%H:%M:%S") if a.timestamp else "??:??:??"
        cls = {
            "eval": "tag-eval",
            "tool": "tag-tool",
            "thinking": "tag-think",
            "heartbeat": "tag-beat",
        }.get(a.kind, "")
        label = a.kind.upper()
        rows.append(
            f'<div><span class="tag {cls}">[{label}]</span> '
            f'<span class="tag">{ts}</span> {html.escape(a.detail)}</div>'
        )
    return "\n".join(rows)


def _tool_rows_html(parsed: ParsedLog) -> str:
    counts = Counter(t.name for t in parsed.tool_calls).most_common()
    return "\n".join(f"<tr><td>{html.escape(n)}</td><td>{c}</td></tr>" for n, c in counts)


def render_html(
    parsed: ParsedLog,
    evals: list[dict[str, Any]],
    bucket_minutes: int,
) -> str:
    scored = [r for r in evals if r["score"] is not None]
    best = max(scored, key=lambda r: r["score"]) if scored else None

    duration_min = (
        ((parsed.run_ended - parsed.run_started).total_seconds() / 60.0)
        if (parsed.run_started and parsed.run_ended)
        else 0.0
    )

    distinct_sessions = len({s.session_id for s in parsed.sessions if s.session_id})
    implied_in_rate = (
        parsed.total_cost / parsed.total_input * 1e6 if parsed.total_input else 0.0
    )
    stats = [
        ("Run duration", f"{duration_min:.1f} min"),
        ("Evals", str(len(parsed.evals))),
        ("Best score", f"{best['score']:.4f}" if best else "—"),
        ("API segments", f"{len(parsed.sessions)} ({distinct_sessions} CC sessions)"),
        ("Input tokens", f"{parsed.total_input:,}"),
        ("Output tokens", f"{parsed.total_output:,}"),
        ("Cache read tokens", f"{parsed.total_cache_read:,}"),
        ("SDK est. cost", f"${parsed.total_cost:.2f}"),
        ("Implied $/M input", f"${implied_in_rate:.2f}"),
    ]

    # Series for charts
    scores_x = [r["eval"] for r in evals if r["score"] is not None]
    scores_y = [r["score"] for r in evals if r["score"] is not None]
    best_x = [best["eval"]] if best else []
    best_y = [best["score"]] if best else []

    cum_input, cum_output, cum_cache, cum_cost = 0, 0, 0, 0.0
    times, ci, co, cc, cu = [], [], [], [], []
    for s in sorted(parsed.sessions, key=lambda x: x.end_time or datetime.min.replace(tzinfo=timezone.utc)):
        if not s.end_time:
            continue
        cum_input += s.input_tokens
        cum_output += s.output_tokens
        cum_cache += s.cache_read
        cum_cost += s.cost_usd
        times.append(s.end_time.isoformat())
        ci.append(cum_input)
        co.append(cum_output)
        cc.append(cum_cache)
        cu.append(round(cum_cost, 6))

    buckets = make_buckets(parsed, bucket_minutes)
    # X labels are the right-edge of each bucket — i.e. "tokens spent so far at minute N".
    bucket_labels = [str(b["bucket_end_min"]) for b in buckets]
    bucket_input_cum = [b["input_cum"] for b in buckets]
    bucket_output_cum = [b["output_cum"] for b in buckets]
    bucket_cache_cum = [b["cache_read_cum"] for b in buckets]
    bucket_cost_cum = [round(b["cost_cum"], 6) for b in buckets]

    tool_counter = Counter(t.name for t in parsed.tool_calls).most_common()
    tool_labels = [t for t, _ in tool_counter]
    tool_values = [c for _, c in tool_counter]

    subtitle_parts = []
    if parsed.run_started:
        subtitle_parts.append(f"start {parsed.run_started.strftime('%Y-%m-%d %H:%M:%S')}")
    subtitle_parts.append(f"{len(parsed.sessions)} sessions")
    subtitle_parts.append(f"${parsed.total_cost:.2f}")
    subtitle_parts.append(f"{parsed.total_tokens:,} tokens")

    return HTML_TMPL.format(
        title=f"CORAL run · {html.escape(parsed.benchmark)}",
        subtitle=" · ".join(subtitle_parts),
        stat_cards="".join(_stat_card(l, v) for l, v in stats),
        eval_rows=_eval_rows_html(evals),
        session_rows=_session_rows_html(parsed),
        tool_rows=_tool_rows_html(parsed),
        activity_rows=_activity_rows_html(parsed),
        bucket_label=str(bucket_minutes),
        scores_json=json.dumps(
            {"x": scores_x, "y": scores_y, "best_x": best_x, "best_y": best_y}
        ),
        tokens_ts_json=json.dumps(
            {"x": times, "input_cum": ci, "output_cum": co, "cache_cum": cc}
        ),
        cost_ts_json=json.dumps({"x": times, "cum": cu}),
        buckets_json=json.dumps(
            {
                "labels": bucket_labels,
                "input_cum": bucket_input_cum,
                "output_cum": bucket_output_cum,
                "cache_cum": bucket_cache_cum,
                "cost_cum": bucket_cost_cum,
            }
        ),
        tools_json=json.dumps({"labels": tool_labels, "values": tool_values}),
        tok_in=parsed.total_input,
        tok_out=parsed.total_output,
        tok_cache=parsed.total_cache_read,
    )


# -------- main --------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("log", type=Path, help="Path to log.txt")
    p.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=Path("analysis"),
        help="Root output directory (default: ./analysis)",
    )
    p.add_argument(
        "--bucket-minutes",
        type=int,
        default=5,
        help="Time-bucket size for the per-bucket charts (default: 5)",
    )
    args = p.parse_args(argv)

    if not args.log.exists():
        print(f"error: log file not found: {args.log}", file=sys.stderr)
        return 1

    parsed = parse_log(args.log)
    out_dir = args.output_root / parsed.benchmark
    out_dir.mkdir(parents=True, exist_ok=True)

    evals = build_eval_table(parsed)

    write_csv(out_dir / "evals.csv", evals)
    write_csv(
        out_dir / "sessions.csv",
        [
            {
                "segment": s.index,
                "session_id": s.session_id,
                "end_time": s.end_time.isoformat() if s.end_time else "",
                "duration_s": round(s.duration_ms / 1000, 2),
                "duration_api_s": round(s.duration_api_ms / 1000, 2),
                "num_turns": s.num_turns,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read": s.cache_read,
                "cache_creation": s.cache_creation,
                "cost_usd": round(s.cost_usd, 6),
                "is_error": s.is_error,
                "stop_reason": s.stop_reason,
                "terminal_reason": s.terminal_reason,
            }
            for s in parsed.sessions
        ],
    )

    # Cumulative timeline CSV
    cum_in = cum_out = cum_cache = 0
    cum_cost = 0.0
    timeline_rows: list[dict[str, Any]] = []
    for s in sorted(parsed.sessions, key=lambda x: x.end_time or datetime.min.replace(tzinfo=timezone.utc)):
        if not s.end_time:
            continue
        cum_in += s.input_tokens
        cum_out += s.output_tokens
        cum_cache += s.cache_read
        cum_cost += s.cost_usd
        timeline_rows.append(
            {
                "time": s.end_time.isoformat(),
                "session": s.index,
                "input_tokens_cum": cum_in,
                "output_tokens_cum": cum_out,
                "cache_read_cum": cum_cache,
                "cost_usd_cum": round(cum_cost, 6),
            }
        )
    write_csv(out_dir / "timeline.csv", timeline_rows)

    # Buckets
    bucket_rows = []
    for size in (5, 10, 15):
        for r in make_buckets(parsed, size):
            bucket_rows.append({"bucket_size_min": size, **r})
    write_csv(out_dir / "buckets.csv", bucket_rows)

    # Tools
    tool_counts = Counter(t.name for t in parsed.tool_calls)
    write_csv(
        out_dir / "tools.csv",
        [{"tool": t, "count": c} for t, c in tool_counts.most_common()],
    )

    write_summary(parsed, evals, out_dir / "summary.txt")
    write_activity(parsed, out_dir / "activity.txt")

    html_doc = render_html(parsed, evals, args.bucket_minutes)
    (out_dir / "report.html").write_text(html_doc, encoding="utf-8")

    distinct_sessions = len({s.session_id for s in parsed.sessions if s.session_id})
    print(f"Analyzed {args.log} → {out_dir}/")
    print(f"  benchmark    : {parsed.benchmark}")
    print(f"  evals        : {len(parsed.evals)} (coral eval submissions)")
    print(
        f"  API segments : {len(parsed.sessions)} "
        f"(across {distinct_sessions} Claude Code session{'s' if distinct_sessions != 1 else ''})"
    )
    print(f"  cost         : ${parsed.total_cost:.4f}")
    print(f"  tokens       : {parsed.total_tokens:,}")
    print(f"  open         : {out_dir / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
