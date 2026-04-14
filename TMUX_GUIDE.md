# Hướng dẫn dùng tmux với CORAL

## Tmux là gì?

Tmux là "terminal ảo chạy nền" — độc lập với VS Code, terminal window, hay bất kỳ ứng dụng nào. Bạn có thể đóng terminal, tắt VS Code, rồi quay lại attach vào tmux bất lúc nào mà không mất tiến trình.

```
macOS
├── VS Code / Terminal    ← đóng cái này không ảnh hưởng gì
└── tmux server           ← chạy độc lập, gắn vào OS
    ├── session: coral-tsp-1agent  → agent vẫn chạy
    └── session: coral-tsp-2agents → agent vẫn chạy
```

**Lưu ý quan trọng:** Máy **sleep** thì tmux bị đóng băng (không crash, nhưng agent API call sẽ timeout). Dùng `caffeinate` nếu muốn chạy qua đêm (xem phần cuối).

---

## Chạy CORAL với tmux

CORAL tự tạo tmux session khi không dùng `run.session=local`:

```bash
# Chạy bình thường → tự tạo tmux session tên "coral-<taskname>"
uv run coral start -c examples/tsp/task.yaml

# Chạy 2 experiment song song (mỗi cái 1 session riêng)
uv run coral start -c examples/tsp/task_1agent.yaml
uv run coral start -c examples/tsp/task_2agents.yaml

# Chạy với web UI trên port khác nhau
uv run coral start -c examples/tsp/task_1agent.yaml run.ui=true
uv run coral ui --run results/tsp-2agents/latest --port 8421
```

---

## Lệnh cơ bản

### Xem sessions đang chạy
```bash
tmux ls
```
```
coral-tsp-1agent: 1 windows (created ...)
coral-tsp-2agents: 1 windows (created ...)
coral-monitor: 1 windows (created ...)
```

### Vào xem session
```bash
tmux attach -t coral-tsp-1agent
tmux attach -t coral-tsp-2agents
tmux attach -t coral-monitor
```

### Thoát session mà KHÔNG kill (detach)
```
Ctrl+B  rồi  D
```
→ Về terminal thường, mọi thứ vẫn chạy nền.

### Kill 1 session (dừng experiment đó)
```bash
tmux kill-session -t coral-tsp-1agent
```

### Kill tất cả sessions
```bash
tmux kill-server
```

---

## Chia màn hình xem nhiều thứ cùng lúc

Khi đang trong 1 session, có thể chia pane để xem song song:

```
Ctrl+B  rồi  %     → chia đôi trái / phải
Ctrl+B  rồi  "     → chia đôi trên / dưới
Ctrl+B  rồi  ←→↑↓  → di chuyển sang pane khác
Ctrl+B  rồi  Z     → zoom pane hiện tại (phóng to / thu nhỏ)
Ctrl+B  rồi  X     → đóng pane hiện tại
```

**Ví dụ: xem 2 experiment cạnh nhau**
```bash
# Tạo session monitor với 2 pane
tmux new-session -d -s coral-monitor
tmux send-keys -t coral-monitor "watch -n 3 'uv run coral status --run results/tsp-1agent/latest'" Enter
tmux split-window -h -t coral-monitor
tmux send-keys -t coral-monitor "watch -n 3 'uv run coral status --run results/tsp-2agents/latest'" Enter
tmux attach -t coral-monitor
```

---

## Scroll xem log cũ

Khi trong session, output chạy nhanh quá:
```
Ctrl+B  rồi  [     → vào scroll mode
↑ ↓              → cuộn lên/xuống
PgUp / PgDn      → cuộn nhanh
q                → thoát scroll mode
```

---

## Theo dõi điểm số mà không cần vào session

```bash
# Xem leaderboard live (cập nhật mỗi 3 giây)
watch -n 3 'uv run coral status'

# Xem log attempts
uv run coral log

# Xem cả 2 experiment song song trong terminal thường
watch -n 5 'echo "=== 1-AGENT ===" && uv run coral log --run results/tsp-1agent/latest -n 5 && echo && echo "=== 2-AGENTS ===" && uv run coral log --run results/tsp-2agents/latest -n 5'
```

---

## Xem 2 Web UI song song

```bash
# UI experiment 1 (port 8420)
uv run coral ui --run results/tsp-1agent/latest --port 8420 --no-open

# UI experiment 2 (port 8421)
uv run coral ui --run results/tsp-2agents/latest --port 8421 --no-open
```

Mở browser:
- http://localhost:8420 → experiment 1
- http://localhost:8421 → experiment 2

---

## Chạy qua đêm (tránh máy sleep)

```bash
# Giữ máy thức trong 8 tiếng (28800 giây)
caffeinate -t 28800 &

# Giữ thức mãi cho đến khi tắt tay
caffeinate &

# Tắt caffeinate
killall caffeinate
```

`caffeinate` có sẵn trên macOS, không cần cài thêm.

---

## Quy trình chạy thực nghiệm hoàn chỉnh

### Bước 1 — Khởi động

```bash
# Giữ máy thức nếu chạy qua đêm
caffeinate &

# Chạy 2 experiment song song (mỗi cái tự tạo 1 tmux session riêng)
uv run coral start -c examples/tsp/task_1agent.yaml
uv run coral start -c examples/tsp/task_2agents.yaml
```

Lúc này có 2 tmux sessions đang chạy nền:
```
coral-tsp-1agent   → 1 agent đang optimize
coral-tsp-2agents  → 2 agent đang optimize song song
```

### Bước 2 — Theo dõi (tùy chọn)

```bash
# Xem sessions nào đang chạy
tmux ls

# Vào xem log trực tiếp của 1 experiment
tmux attach -t coral-tsp-1agent
# → Ctrl+B D để thoát ra mà không kill

# Xem leaderboard nhanh không cần vào session
uv run coral log
uv run coral status
```

### Bước 3 — Dừng

#### Cách 1: Dừng agent nhưng giữ nguyên kết quả (khuyến nghị)

```bash
uv run coral stop          # gửi signal dừng agent gracefully
tmux kill-server           # xóa tất cả tmux sessions
killall caffeinate         # tắt chế độ giữ thức máy
```

#### Cách 2: Vào session rồi Ctrl+C

```bash
tmux attach -t coral-tsp-1agent
# Nhấn Ctrl+C → agent dừng, session vẫn còn
tmux kill-session -t coral-tsp-1agent   # xóa session nếu muốn
```

> Sau khi stop, toàn bộ attempts/notes/skills **vẫn còn nguyên** trong thư mục `results/`. Không mất dữ liệu.

### Bước 4 — Tiếp tục (Resume)

CORAL lưu toàn bộ trạng thái vào `results/<taskname>/latest/`:

- **Git worktrees** của từng agent (code hiện tại)
- **`.coral/public/attempts/`** — toàn bộ lịch sử eval
- **`.coral/public/notes/`** — notes agent đã viết
- **`.coral/public/skills/`** — skills agent đã chia sẻ

```bash
# Resume experiment gần nhất (tự detect run trong results/)
uv run coral resume

# Resume với config khác
uv run coral resume agents.model=claude-opus-4-6

# Resume với thêm hướng dẫn cho agent
uv run coral resume -i "Tập trung vào thuật toán 3-opt và LKH"

# Resume với số agent nhiều hơn
uv run coral resume agents.count=4
```

#### Resume 2 experiment song song với tmux

```bash
# Chỉ định run path cụ thể cho từng experiment
uv run coral resume --run results/tsp-1agent/latest
uv run coral resume --run results/tsp-2agents/latest
```

> `coral resume` tự tạo tmux session mới tên `coral-<taskname>` như lần đầu, agent tiếp tục từ đúng session Claude Code đã bị dừng.

### Bước 5 — Xem kết quả cuối

```bash
# Leaderboard top 20
uv run coral log

# 5 attempt gần nhất
uv run coral log -n 5 --recent

# Xem chi tiết 1 attempt (dùng hash từ log)
uv run coral show <hash>
uv run coral show <hash> --diff    # kèm code diff đầy đủ

# Web UI
uv run coral ui
```

---

## Cheat sheet nhanh

| Việc muốn làm | Lệnh |
|---|---|
| Xem sessions | `tmux ls` |
| Vào session | `tmux attach -t <tên>` |
| Thoát (không kill) | `Ctrl+B` → `D` |
| Kill 1 session | `tmux kill-session -t <tên>` |
| Kill tất cả | `tmux kill-server` |
| Chia đôi trái/phải | `Ctrl+B` → `%` |
| Chia đôi trên/dưới | `Ctrl+B` → `"` |
| Chuyển pane | `Ctrl+B` → `←→↑↓` |
| Scroll log cũ | `Ctrl+B` → `[` rồi `q` thoát |
| Zoom pane | `Ctrl+B` → `Z` |
| Giữ máy thức | `caffeinate &` |
