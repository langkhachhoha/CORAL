# CORAL - Bài giảng chi tiết cho người mới

> Mục tiêu: Sau khi đọc xong file này, bạn sẽ hiểu **chính xác** từng dòng code quan trọng của CORAL và cách toàn bộ hệ thống ghép lại với nhau.

---

## Phần 1: Ý tưởng cốt lõi (5 phút)

Hãy tưởng tượng bạn muốn giải một bài toán khó (ví dụ TSP - tìm đường đi ngắn nhất qua 100 thành phố). Thay vì tự viết code, bạn muốn **thuê nhiều AI cùng làm** và cạnh tranh nhau để tìm giải pháp tốt nhất.

CORAL là "công ty thuê AI" đó. Nó:
1. **Tuyển** N con AI (Claude/Codex/OpenCode)
2. **Giao** mỗi con một bản copy code + hướng dẫn
3. **Chấm điểm** mỗi lần chúng submit
4. **Ghi bảng xếp hạng** để chúng học lẫn nhau
5. **Lặp mãi mãi** cho đến khi bạn bảo dừng

**Metaphor:** CORAL giống như Kaggle competition — nhưng thí sinh là AI, chạy 24/7, và chúng đọc được submission của nhau.

---

## Phần 2: Cấu trúc dữ liệu cơ bản

Trước khi đọc code, hiểu **4 kiểu dữ liệu** chính trong [coral/types.py](coral/types.py):

### 2.1. `Task` - Bài toán

```python
@dataclass
class Task:
    id: str                 # "tsp", "circle_packing"
    name: str               # tên hiển thị
    description: str        # mô tả cho AI đọc
    metadata: dict[str, Any] = field(default_factory=dict)
```

**Giải thích từng dòng:**
- `@dataclass`: Python magic — tự tạo `__init__`, `__repr__`, `__eq__`. Bạn chỉ cần khai báo fields.
- `id: str`: ID duy nhất. AI không thấy field này.
- `description: str`: **Đây là prompt AI đọc**. Ví dụ: "Find shortest route through 100 cities..."
- `metadata: dict`: Dữ liệu phụ (không bắt buộc).

### 2.2. `Score` - Điểm số

```python
@dataclass
class Score:
    value: float | str | bool | None    # điểm: có thể là số, text, hoặc bool
    name: str                            # tên loại điểm: "accuracy", "distance"
    explanation: str | None = None       # lý do cho điểm này
```

**Tại sao `value` lại có nhiều kiểu?**
- `float`: điểm số thông thường (-7.97 cho TSP)
- `bool`: đúng/sai (cho bài yes/no)
- `str`: "CORRECT"/"INCORRECT" (cho bài chấm qua LLM judge)
- `None`: grader crash, không chấm được

Hàm `to_float()` chuyển mọi kiểu về số để so sánh leaderboard:

```python
def to_float(self) -> float | None:
    if self.value is None:
        return None
    if isinstance(self.value, bool):
        return 1.0 if self.value else 0.0
    elif isinstance(self.value, int | float):
        return float(self.value)
    elif isinstance(self.value, str):
        mapping = {"CORRECT": 1.0, "INCORRECT": 0.0, "PARTIAL": 0.5, ...}
        return mapping.get(self.value.upper(), 0.0)
```

### 2.3. `ScoreBundle` - Gói điểm

Một lần chấm điểm có thể cho **nhiều điểm** (ví dụ: accuracy + latency + code quality). `ScoreBundle` gom chúng lại:

```python
@dataclass
class ScoreBundle:
    scores: dict[str, Score]           # {"accuracy": Score(...), "latency": Score(...)}
    aggregated: float | None = None    # điểm tổng hợp (weighted average)
    feedback: str | None = None        # nhận xét chung cho agent
```

Ví dụ TSP chỉ có 1 điểm:
```python
ScoreBundle(
    scores={"distance": Score(value=-7.97, name="distance")},
    aggregated=-7.97,
    feedback="Used nearest-neighbor + 2-opt"
)
```

### 2.4. `Attempt` - Bản ghi 1 lần submit

```python
@dataclass
class Attempt:
    commit_hash: str      # git commit hash của code lúc đó
    agent_id: str         # "agent-1"
    title: str            # message mô tả
    score: float | None   # điểm tổng hợp (hoặc None nếu crash)
    status: str           # "improved" | "regressed" | "crashed" | "baseline"
    parent_hash: str | None  # commit trước (để so sánh cải thiện)
    timestamp: str
    feedback: str = ""
```

**Key insight**: `Attempt` là **đơn vị lưu trữ chính** của CORAL. Mỗi lần agent chạy `coral eval`, một `Attempt` được tạo và lưu thành JSON file vào `.coral/public/attempts/<commit_hash>.json`. Tất cả agent khác đều đọc được.

---

## Phần 3: Cấu hình task (task.yaml)

Xem [examples/tsp/task.yaml](examples/tsp/task.yaml):

```yaml
task:
  name: tsp
  description: |
    Find the shortest round-trip tour through 100 cities...

grader:
  type: function
  module: eval.grader

agents:
  count: 1
  runtime: claude_code
  model: claude-sonnet-4-6
  max_turns: 100

workspace:
  results_dir: "./results"
  repo_path: "./examples/tsp/seed"
```

File này được parse bởi `CoralConfig.from_yaml()` thành các **dataclass** trong [coral/config.py](coral/config.py):

### 3.1. `task:` section

```python
@dataclass
class TaskConfig:
    name: str = MISSING           # bắt buộc
    description: str = MISSING    # bắt buộc
    tips: str = ""                # gợi ý thêm cho AI (tuỳ chọn)
```

`MISSING` (từ thư viện `omegaconf`) nghĩa là: nếu YAML không có → **lỗi ngay**. Đây là cách ép buộc user khai báo field bắt buộc.

### 3.2. `grader:` section

```python
@dataclass
class GraderConfig:
    type: str = ""              # "function" = gọi hàm Python, rỗng = auto-discover
    module: str = ""            # "eval.grader" → import eval/grader.py
    timeout: int = 300          # kill grader sau 300s nếu chưa xong
    direction: str = "maximize" # "maximize" hoặc "minimize"
    private: list[str] = []     # files ẩn với agent (answer key!)
```

**Tại sao có `private`?** Ví dụ bài ML classification: answer key `test_labels.csv` phải giấu khỏi agent, không chúng sẽ cheat. `private: [test_labels.csv]` sẽ copy file này vào `.coral/private/` (mà `settings.json` cấm agent đọc).

### 3.3. `agents:` section

```python
@dataclass
class AgentConfig:
    count: int = 1                   # số agent song song
    runtime: str = "claude_code"     # claude_code | codex | opencode
    model: str = "sonnet"            # model name
    max_turns: int = 200             # số tool call trước khi reboot session
    timeout: int = 3600              # kill agent sau 3600s
    heartbeat: list[HeartbeatActionConfig] = [...]  # reminder tự động
    research: bool = True            # cho phép WebSearch/WebFetch
    stagger_seconds: int = 0         # delay giữa các agent spawn
```

**Đây là phần quan trọng nhất để hiểu vì sao agent hôm qua chỉ làm 1 attempt:**

- `max_turns: 100`: Sau 100 tool calls (Bash, Read, Write, Edit, ...), **session hiện tại bị dừng và spawn session mới**. Mỗi session là một subprocess Claude Code mới với context riêng.
- Lý do reboot: tránh context overflow, fresh memory. Nhưng session mới phải "orientate" lại (đọc CORAL.md, coral log, ls .claude/notes, ...) → tốn turns.
- Nếu đặt `max_turns: 10` → 5-7 turns đầu đã hết cho orientation, chỉ còn ~2-3 turns để edit + eval. Đó là lý do hôm qua chỉ có 1 attempt.

### 3.4. `workspace:` section

```python
@dataclass
class WorkspaceConfig:
    results_dir: str = "./results"   # nơi lưu mọi run
    repo_path: str = "./repo"        # code gốc (seed)
```

---

## Phần 4: Luồng hoạt động — `coral start`

Khi bạn gõ `uv run coral start -c examples/tsp/task.yaml`, đây là chuỗi sự kiện:

### Bước 4.1: Parse config

```python
config = CoralConfig.from_yaml("examples/tsp/task.yaml")
```

YAML → dataclass tree. Nếu thiếu field bắt buộc → crash ngay.

### Bước 4.2: Tạo project structure

[coral/agent/manager.py:75](coral/agent/manager.py#L75):
```python
self.paths = create_project(self.config, config_dir=self.config_dir)
```

`create_project()` sẽ:
1. Tạo thư mục `results/tsp/2026-04-14_103549/` (timestamp = run ID)
2. Bên trong có:
   - `.coral/` - shared state (attempts, notes, skills, private)
   - `repo/` - bản copy code từ `repo_path` (git init)
   - `agents/` - sẽ chứa worktree của từng agent
   - `config.yaml` - bản copy config để agent resume đọc được

**Vì sao copy config vào `.coral/`?** Vì khi agent gọi `coral eval`, hàm `run_eval()` cần tìm config. Nó walk up từ worktree tìm file `.coral_dir`, rồi đọc `.coral/config.yaml`.

### Bước 4.3: Validate grader

Manager load grader (Python module) một lần để chắc chắn nó import được. Nếu grader có bug syntax → crash ngay, không spawn agent.

### Bước 4.4: Tạo worktree cho mỗi agent

Với mỗi agent `i` trong `range(config.agents.count)`:

```python
# coral/workspace/worktree.py
def create_agent_worktree(repo_path, agent_id, agents_dir):
    branch_name = f"coral/{agent_id}"          # "coral/agent-1"
    worktree_path = agents_dir / agent_id       # "agents/agent-1/"
    
    # 1. Tạo branch mới từ HEAD hiện tại
    git branch coral/agent-1 HEAD
    
    # 2. Tạo worktree (linked checkout)
    git worktree add agents/agent-1 coral/agent-1
```

**Git worktree là gì?** Là một "bản checkout riêng" của cùng 1 git repo. Nhiều worktree chia sẻ cùng `.git/` objects (tiết kiệm disk) nhưng có **branch độc lập, working directory độc lập**.

→ Agent-1 commit vào `coral/agent-1`, agent-2 commit vào `coral/agent-2`, không đụng nhau.

### Bước 4.5: Symlink shared state

```python
def setup_shared_state(worktree_path, coral_dir):
    shared_dir = worktree_path / ".claude"    # ".claude/" trong worktree
    shared_dir.mkdir()
    
    for item in ["notes", "skills", "attempts", "logs", "heartbeat"]:
        src = coral_dir / "public" / item       # .coral/public/notes
        dst = shared_dir / item                 # agents/agent-1/.claude/notes
        dst.symlink_to(src)
```

**Kết quả:** Khi agent-1 đọc `.claude/notes/`, thực chất nó đọc `.coral/public/notes/` — nơi mọi agent cùng ghi vào. Đây là **cơ chế chia sẻ kiến thức**.

### Bước 4.6: Viết `settings.json` cho permissions

[coral/workspace/worktree.py:163](coral/workspace/worktree.py#L163):
```python
def setup_claude_settings(worktree_path, coral_dir, ...):
    allow_rules = [
        "Bash",                                   # không scope → tất cả bash
        f"Read(/{worktree_pattern})",             # đọc worktree của mình
        f"Read(/{agents_pattern})",               # đọc worktree agent khác
        f"Read(/{public_pattern})",               # đọc .coral/public
        f"Edit(/{worktree_pattern})",             # sửa file trong worktree
        f"Edit(/{public_pattern})",               # sửa notes/skills chung
        f"Write(/{worktree_pattern})",
        f"Write(/{public_pattern})",
    ]
    deny_rules = [
        "Bash(git *)",                            # CẤM git trực tiếp
        f"Read(/{private_pattern})",              # CẤM đọc answer key
    ]
```

**Vì sao cấm `git`?** Vì agent phải dùng `coral eval` để commit — cái đó sẽ tự động chấm điểm và lưu attempt. Nếu agent `git commit` trực tiếp, sẽ không có score → phá vỡ loop.

### Bước 4.7: Generate `CLAUDE.md`

[coral/template/coral_md.py](coral/template/coral_md.py):
```python
def generate_coral_md(config, agent_id, ...):
    template = _TEMPLATE_PATH.read_text()
    return template.format(
        task_name=config.task.name,
        task_description=config.task.description,
        agent_id=agent_id,
        score_direction=..., 
        ...
    )
```

File `CLAUDE.md` là **prompt hệ thống** mà Claude Code đọc khi khởi động. Nó chứa:
- Mô tả bài toán
- Hướng dẫn workflow (plan → edit → eval → repeat)
- CLI commands (`coral log`, `coral eval`, ...)
- Rules (không dùng git trực tiếp, phải viết notes sau mỗi eval, ...)

Đây là "cẩm nang nhân viên" của agent.

### Bước 4.8: Spawn subprocess

[coral/agent/runtime.py](coral/agent/runtime.py):
```python
def start(worktree_path, coral_md_path, model, max_turns, ...):
    cmd = [
        "claude", "code",
        "--model", model,
        "--max-turns", str(max_turns),
        "--prompt", "Begin.",      # prompt khởi động
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=worktree_path,          # chạy trong worktree
        stdout=log_file,
        stderr=log_file,
    )
    return AgentHandle(agent_id, proc, worktree_path, log_path)
```

Claude Code khởi động, đọc `CLAUDE.md` tự động (vì file nằm trong `cwd`), rồi bắt đầu làm việc.

### Bước 4.9: Manager monitor loop

Manager chạy thread nền:
```python
while self._running:
    for handle in self.handles:
        if not handle.alive:
            # Agent chết → restart
            self._restart_agent(handle.agent_id)
        
        # Heartbeat: mỗi N eval thì inject prompt
        if eval_count % interval == 0:
            self._send_heartbeat(handle, action="reflect")
    
    time.sleep(5)
```

---

## Phần 5: Vòng lặp của Agent

Sau khi spawn, agent tự chạy vòng lặp này (theo CLAUDE.md):

```
┌──────────────────────────────────────────────────┐
│  Turn 1-5:  ORIENT                               │
│    - Đọc CLAUDE.md (automatic)                   │
│    - coral log (xem leaderboard)                 │
│    - coral log --recent                          │
│    - ls .claude/notes/                           │
│    - ls .claude/skills/                          │
│    - cat solution.py                             │
│                                                  │
│  Turn 6-10: RESEARCH (lần đầu)                   │
│    - WebSearch "TSP nearest neighbor 2-opt"      │
│    - Đọc 1-2 bài viết                            │
│                                                  │
│  Turn 11-15: PLAN + EDIT                         │
│    - Nghĩ: thử nearest neighbor + 2-opt          │
│    - Edit solution.py                            │
│                                                  │
│  Turn 16:   EVAL                                 │
│    - coral eval -m "nearest neighbor + 2-opt"    │
│    - Kết quả: score=-7.97                        │
│                                                  │
│  Turn 17-18: LEARN                               │
│    - Viết note: .claude/notes/what-worked.md     │
│    - (có thể) tạo skill                          │
│                                                  │
│  Turn 19-25: PLAN NEXT                           │
│    - coral log để xem có agent khác tốt hơn ko   │
│    - Nghĩ: thử simulated annealing?              │
│    - Edit solution.py                            │
│                                                  │
│  Turn 26:   EVAL                                 │
│    - coral eval -m "simulated annealing"         │
│    ...                                           │
│                                                  │
│  Turn 100:  SESSION END                          │
│    - Session reboot, context mới                 │
└──────────────────────────────────────────────────┘
```

---

## Phần 6: `coral eval` — Điều kỳ diệu xảy ra ở đây

Khi agent chạy `coral eval -m "nearest neighbor + 2-opt"`, function [run_eval()](coral/hooks/post_commit.py#L153) thực thi:

### 6.1. Tìm `.coral/` directory

```python
workdir_path = Path(".").resolve()   # agents/agent-1/
coral_dir = _find_coral_dir(workdir_path)
```

`_find_coral_dir` đọc file `.coral_dir` (breadcrumb) trong worktree để biết đường dẫn tới shared `.coral/`.

### 6.2. Git add + commit

```python
def _git_add_and_commit(message, workdir):
    subprocess.run(["git", "add", "-A"], cwd=workdir)       # stage all
    
    # Check có gì commit không
    if git diff --cached --quiet:
        raise RuntimeError("Nothing to commit")
    
    subprocess.run(["git", "commit", "-m", message], cwd=workdir)
    
    # Lấy hash vừa commit
    return git rev-parse HEAD
```

**Mẹo quan trọng:** Dù agent bị cấm chạy `git` trực tiếp (deny rule), nhưng function này là **Python code của CORAL** chạy TRONG cùng process với agent — nó bypass permission của Claude Code.

### 6.3. Chạy grader trong process riêng

```python
def _run_grader_with_timeout(config_path, coral_dir, codebase_path, tasks, timeout):
    result_queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_grader_worker,
        args=(config_path, coral_dir, codebase_path, tasks, result_queue),
    )
    proc.start()
    proc.join(timeout=timeout)
    
    if proc.is_alive():
        proc.kill()                              # kill nếu overtime
        raise TimeoutError(f"Grader timed out after {timeout}s")
    
    status, *payload = result_queue.get_nowait()
    return payload[0]
```

**Tại sao phải dùng `multiprocessing`?** Vì:
1. Grader có thể block trong C code (numpy, torch) → `asyncio.wait_for` không kill được.
2. Grader có thể crash → process riêng không ảnh hưởng main.
3. Timeout cứng — quá giờ thì `proc.kill()` dứt khoát.

### 6.4. Grader tính điểm

```python
# eval/grader.py
class Grader(TaskGrader):
    def evaluate(self) -> float:
        result = self.run_program("solution.py")       # chạy solution.py
        order = [int(x) for x in result.stdout.strip().split("\n")]
        assert sorted(order) == list(range(100))       # validate
        dist = sum(math.dist(CITIES[order[i]], CITIES[order[(i+1) % 100]])
                   for i in range(100))
        return -dist        # âm để "bigger is better"
```

Grader kế thừa `TaskGrader`, có các helper:
- `self.run_program(filename)`: chạy `solution.py`, trả về `CompletedProcess`
- `self.fail(reason)`: ghi failure + trả `None` score
- `self.codebase_path`: đường dẫn tới code của agent (worktree)
- `self.private_dir`: đường dẫn tới `.coral/private/` (answer key)

### 6.5. So sánh với attempts cũ

```python
prev_attempts = get_agent_attempts(coral_dir, agent_id)
prev_scores = [a.score for a in prev_attempts if a.score is not None]
minimize = config.grader.direction == "minimize"

if not prev_scores:
    status = "baseline"      # attempt đầu tiên
elif minimize:
    best = min(prev_scores)
    status = "improved" if score < best else "regressed"
else:
    best = max(prev_scores)
    status = "improved" if score > best else "regressed"
```

### 6.6. Ghi Attempt ra JSON

```python
attempt = Attempt(
    commit_hash=commit_hash,
    agent_id=agent_id,
    title=message,
    score=score,
    status=status,
    parent_hash=parent_hash,
    timestamp=datetime.now(UTC).isoformat(),
    feedback=feedback,
)
write_attempt(coral_dir, attempt)
```

File `attempts/<hash>.json` được tạo. **Lập tức** mọi agent khác đọc được (vì symlink vào `.claude/attempts/`).

### 6.7. In ra màn hình

```python
print(f"Score: {score}")
print(f"Status: {status}")
print(f"Feedback: {feedback}")
```

Agent thấy output này trong terminal → dùng để quyết định bước tiếp theo.

---

## Phần 7: Cơ chế chia sẻ (Collaboration)

Điểm quan trọng nhất của CORAL vs "chạy 1 AI":

### 7.1. Attempts

Khi agent-1 eval xong → `.coral/public/attempts/abc123.json`.

Agent-2 chạy `coral log` → đọc tất cả JSON trong `.coral/public/attempts/` → hiển thị bảng xếp hạng có cả attempts của agent-1.

Agent-2 thấy "abc123 có score -7.97 (cao nhất)" → chạy `coral show abc123 --diff` → xem CODE CỦA AGENT-1 → học.

### 7.2. Notes (ghi chú)

Format: markdown với YAML frontmatter:
```markdown
---
creator: agent-1
created: 2026-04-14T04:18:33
---
# Nearest neighbor rất tốt làm seed

Tôi thử nearest neighbor từ city 0 → -12.5
Sau đó apply 2-opt → -7.97. 2-opt mang lại cải thiện ~40%.
Khuyến nghị: luôn bắt đầu bằng NN thay vì random.
```

Agent-2 đọc file này → biết tips từ agent-1 mà không cần reverse engineer code.

### 7.3. Skills (công cụ tái sử dụng)

Khi một kỹ thuật chứng minh hiệu quả, agent đóng gói thành **skill** — directory có structure chuẩn:
```
.claude/skills/nearest-neighbor-2opt/
├── SKILL.md         # mô tả: khi nào dùng, cách dùng
├── scripts/
│   └── nn_2opt.py   # code tái sử dụng
└── examples/
    └── example.py
```

Agent-3 tới sau, chạy `ls .claude/skills/` → thấy `nearest-neighbor-2opt` → import `scripts/nn_2opt.py` vào `solution.py`. Không cần viết lại.

### 7.4. Heartbeat

Manager định kỳ inject prompt vào agent:
```python
# Mỗi 1 eval
"Reflect on your last attempt. What worked? What didn't?"

# Mỗi 10 eval toàn cục
"Consolidate your skills. Update SKILL.md with new findings."

# Sau 5 eval không cải thiện (plateau)
"You're stuck. Pivot to a fundamentally different approach."
```

Đây là cơ chế ép agent không bị rơi vào local optimum.

---

## Phần 8: Tại sao có `max_turns`?

Vấn đề: Claude Code có **context window có hạn** (~200K tokens). Sau vài chục tool calls + outputs, context đầy → model bắt đầu "quên" hướng dẫn ban đầu, chất lượng giảm.

Giải pháp CORAL: sau `max_turns` tool calls, **kill subprocess, spawn lại**. Session mới:
- Context trống
- Đọc lại `CLAUDE.md`
- Chạy lại `coral log` để xem leaderboard (biết mình đang ở đâu)
- **Kế thừa git history** (cùng branch `coral/agent-1`), notes, skills

Agent "reboot" nhưng **không mất tiến độ** vì tất cả knowledge ở file.

Đó là lý do `max_turns: 10` quá thấp: session mới tốn 5-7 turns chỉ để orientate, còn 3-5 turns không đủ cho 1 cycle edit+eval.

`max_turns: 100` hợp lý: orient 7 turns + (edit 2 + eval 1 + note 1) × ~23 cycles = 100 turns.

---

## Phần 9: Checklist hiểu bài

Tự trả lời các câu hỏi này để check:

1. **Khi `coral start` chạy, thư mục nào được tạo trước: worktree hay `.coral/`?**
   → `.coral/` trước (trong `create_project`), sau đó mới tới worktree cho từng agent.

2. **Tại sao `.claude/notes/` lại là symlink?**
   → Để nhiều agent cùng ghi vào một chỗ chung `.coral/public/notes/` mà không cần code sync.

3. **Nếu agent chạy `git commit` thì sao?**
   → Bị deny rule chặn. Agent phải dùng `coral eval` — cái này là Python code bypass permission.

4. **Grader chạy trong cùng process với agent?**
   → KHÔNG. Grader chạy trong `multiprocessing.Process` riêng để có timeout cứng và cô lập crash.

5. **Khi agent hit `max_turns`, nó mất những gì?**
   → Mất: context trong memory của session.
   → Còn: git history, attempts JSON, notes, skills.

6. **Làm sao agent-2 biết agent-1 đã thử cái gì?**
   → `coral log` đọc `.coral/public/attempts/*.json`, `coral show <hash>` xem diff.

7. **`ScoreBundle.aggregated` được tính thế nào khi có nhiều score?**
   → `compute_aggregated(weights)`: weighted average các score. Mặc định weight=1.0 đều.

---

## Phần 10: Đọc code theo thứ tự nào?

Nếu bạn muốn đọc sâu codebase, thứ tự tôi khuyên:

1. [coral/types.py](coral/types.py) — 4 dataclass cơ bản (đọc trước)
2. [coral/config.py](coral/config.py) — hiểu YAML → object
3. [coral/grader/task_grader.py](coral/grader/task_grader.py) — grader protocol
4. [coral/hooks/post_commit.py](coral/hooks/post_commit.py) — `run_eval()` (trái tim hệ thống)
5. [coral/workspace/worktree.py](coral/workspace/worktree.py) — setup worktree + settings
6. [coral/template/coral_md.py](coral/template/coral_md.py) — generate CLAUDE.md
7. [coral/agent/manager.py](coral/agent/manager.py) — spawn + monitor loop
8. [coral/agent/runtime.py](coral/agent/runtime.py) — Claude Code subprocess
9. [coral/hub/attempts.py](coral/hub/attempts.py) — attempts CRUD
10. [coral/cli/](coral/cli/) — CLI entry points

---

## Tóm tắt 1 câu

> CORAL = **N con AI × git worktree riêng × thư mục shared × vòng lặp `edit → coral eval → học → lặp`** — được điều phối bởi manager Python, chấm điểm bởi grader chạy trong subprocess cô lập, và chia sẻ kiến thức qua attempts/notes/skills symlink.
