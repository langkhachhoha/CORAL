# CORAL - Luồng hoạt động chi tiết

## Tổng quan

CORAL là hệ thống điều phối nhiều AI agent tự động. Mỗi agent làm việc độc lập trên branch riêng, nhưng chia sẻ kết quả với nhau qua thư mục `.coral/`. Vòng lặp cốt lõi:

```
Tạo workspace -> Agent đọc hướng dẫn -> Sửa code -> Đánh giá -> Lặp lại mãi mãi
```

---

## Bước 1: Chuẩn bị (Người dùng làm)

Bạn cần 3 thứ:

```
examples/tsp/
├── seed/              # Code gốc ban đầu (agent sẽ cải tiến code này)
│   └── solution.py
├── eval/              # Grader để chấm điểm
│   └── grader.py
└── task.yaml          # File cấu hình
```

### task.yaml giải thích:

```yaml
task:
  name: tsp                    # Tên bài toán
  description: |               # Mô tả bài toán (agent sẽ đọc cái này)
    Tìm đường đi ngắn nhất qua 100 thành phố...

grader:
  type: function               # Loại grader (function = gọi hàm Python)
  module: eval.grader           # Đường dẫn đến file grader

agents:
  count: 1                     # Số agent chạy song song
  runtime: claude_code         # Agent nào (claude_code / codex / opencode)
  model: claude-sonnet-4-6     # Model AI sử dụng
  max_turns: 200               # Số lượt tối đa trước khi agent reboot

workspace:
  results_dir: "./results"     # Nơi lưu kết quả
  repo_path: "./examples/tsp/seed"  # Thư mục code gốc
```

---

## Bước 2: Khởi chạy (`coral start`)

Khi bạn chạy `uv run coral start -c task.yaml`, hệ thống thực hiện theo thứ tự:

```
coral start
│
├── 1. Tạo cấu trúc project
│   ├── results/tsp/2026-04-14_103549/    # Thư mục run (theo timestamp)
│   ├── .coral/public/                     # Shared state
│   │   ├── attempts/                      # Kết quả đánh giá (JSON)
│   │   ├── notes/                         # Ghi chú giữa các agent
│   │   └── skills/                        # Công cụ agent tự tạo
│   └── repo/                              # Bản sao code gốc (git init)
│
├── 2. Khởi tạo heartbeat config
│   └── Cấu hình khi nào agent cần "reflect", "consolidate", "pivot"
│
├── 3. [Tuỳ chọn] Warm-start research
│   └── Agent tìm kiếm web, đọc paper trước khi code
│
├── 4. Với MỖI agent (ví dụ agent-1, agent-2, ...):
│   ├── a. Tạo git worktree riêng (branch: coral/agent-1)
│   ├── b. Symlink .coral/public/ vào worktree (để thấy kết quả chung)
│   ├── c. Tạo file CORAL.md (hướng dẫn cho agent)
│   ├── d. Cấu hình permissions (cho agent chạy tự do)
│   └── e. Spawn subprocess Claude Code / Codex / OpenCode
│
├── 5. Ghi PID file (để coral stop biết process nào cần kill)
│
└── 6. Bắt đầu monitor loop (theo dõi agent, auto-restart nếu chết)
```

---

## Bước 3: Agent tự chạy (Vòng lặp vô hạn)

Mỗi agent đọc file `CORAL.md` và tự thực hiện vòng lặp:

```
┌─────────────────────────────────────────────────────┐
│                    VÒNG LẶP AGENT                   │
│                                                     │
│  ┌──────────┐                                       │
│  │ Định hướng│ ← Chỉ lần đầu                       │
│  │          │   - Đọc task description              │
│  │          │   - Xem leaderboard (coral log)       │
│  │          │   - Đọc notes từ agent khác           │
│  │          │   - Xem top attempts (coral show)     │
│  └────┬─────┘                                       │
│       ▼                                             │
│  ┌──────────┐                                       │
│  │ Lên kế   │ - Phân tích điểm cao nhất            │
│  │ hoạch    │ - Nghĩ chiến lược mới                │
│  │          │ - Tham khảo ghi chú agent khác        │
│  └────┬─────┘                                       │
│       ▼                                             │
│  ┌──────────┐                                       │
│  │ Sửa code │ - Thay đổi tập trung 1 ý tưởng      │
│  │          │ - Ưu tiên tốc độ hơn hoàn hảo        │
│  └────┬─────┘                                       │
│       ▼                                             │
│  ┌──────────┐    ┌────────────────────────────┐     │
│  │ Đánh giá │───►│ coral eval -m "mô tả"      │     │
│  │          │    │  1. git add -A              │     │
│  │          │    │  2. git commit              │     │
│  │          │    │  3. Chạy grader (chấm điểm)│     │
│  │          │    │  4. Ghi JSON vào .coral/    │     │
│  │          │    │  5. In điểm cho agent thấy  │     │
│  └────┬─────┘    └────────────────────────────┘     │
│       ▼                                             │
│  ┌──────────┐                                       │
│  │ Phân tích│ - Điểm tăng? Tiếp tục hướng này     │
│  │ kết quả  │ - Điểm giảm? coral revert, thử khác │
│  │          │ - Bế tắc? Đọc notes, đổi chiến lược │
│  └────┬─────┘                                       │
│       │                                             │
│       └──────────── Quay lại "Lên kế hoạch" ◄──────┘
└─────────────────────────────────────────────────────┘
```

---

## Bước 4: Chia sẻ kiến thức (Tự động)

Các agent giao tiếp qua thư mục `.coral/public/` (symlink trong mỗi worktree):

```
.coral/public/
├── attempts/          # Mọi agent đều thấy kết quả của nhau
│   ├── abc123.json    # { commit_hash, agent_id, score, title, feedback }
│   └── def456.json
├── notes/             # Agent ghi lại phát hiện quan trọng
│   └── agent-1_nearest_neighbor_works.md
└── skills/            # Agent đóng gói công cụ hữu ích
    └── 2opt_optimizer/
        └── SKILL.md
```

**Cách hoạt động:**
- Agent-1 tìm ra nearest-neighbor cho điểm cao -> ghi note
- Agent-2 đọc note đó -> kết hợp với 2-opt -> điểm cao hơn
- Agent-3 thấy cả hai attempts -> thử simulated annealing

---

## Bước 5: Heartbeat (Manager theo dõi agent)

Manager chạy nền, kiểm tra agent định kỳ và gửi "heartbeat prompts":

| Heartbeat | Khi nào | Làm gì |
|-----------|---------|--------|
| **reflect** | Mỗi 1 eval | Agent nhìn lại: đang đi đúng hướng không? |
| **consolidate** | Mỗi 10 eval (toàn cục) | Tổng hợp kiến thức, viết notes/skills |
| **pivot** | 5 eval không cải thiện | Buộc agent đổi hướng hoàn toàn |

Nếu agent chết (crash/timeout), manager tự restart nó.

---

## Bước 6: Theo dõi & Dừng

### Theo dõi:
```bash
coral status          # Xem agent nào đang chạy, điểm cao nhất
coral log             # Bảng xếp hạng top 20
coral log --recent    # Xem attempts gần đây
coral show <hash>     # Xem chi tiết 1 attempt + code diff
coral ui              # Mở web dashboard (port 8420)
```

### Dừng:
```bash
coral stop            # Dừng tất cả agent
coral resume          # Chạy lại từ điểm dừng
```

---

## Sơ đồ tổng thể

```
┌─────────────────────────────────────────────────────────────┐
│                        CORAL SYSTEM                         │
│                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐       │
│  │   Agent 1   │   │   Agent 2   │   │   Agent 3   │       │
│  │ branch:     │   │ branch:     │   │ branch:     │       │
│  │ coral/      │   │ coral/      │   │ coral/      │       │
│  │ agent-1     │   │ agent-2     │   │ agent-3     │       │
│  │             │   │             │   │             │       │
│  │ Đọc CORAL.md│   │ Đọc CORAL.md│   │ Đọc CORAL.md│       │
│  │ Sửa code   │   │ Sửa code   │   │ Sửa code   │       │
│  │ coral eval  │   │ coral eval  │   │ coral eval  │       │
│  └──────┬──────┘   └──────┬──────┘   └──────┬──────┘       │
│         │                 │                 │               │
│         ▼                 ▼                 ▼               │
│  ┌─────────────────────────────────────────────────┐       │
│  │              .coral/public/ (shared)             │       │
│  │  attempts/  │  notes/  │  skills/                │       │
│  └─────────────────────────────────────────────────┘       │
│         ▲                                                   │
│         │                                                   │
│  ┌──────┴──────┐    ┌──────────────┐                        │
│  │   Manager   │    │  Web UI      │                        │
│  │ - Monitor   │    │  :8420       │                        │
│  │ - Restart   │    │ - Leaderboard│                        │
│  │ - Heartbeat │    │ - Diffs      │                        │
│  └─────────────┘    └──────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Tóm tắt 1 dòng

> CORAL spawn N agent AI, mỗi agent có branch riêng, tự sửa code + chấm điểm + chia sẻ kết quả, lặp vô hạn cho đến khi bạn dừng.
