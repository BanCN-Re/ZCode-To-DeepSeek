# ZCode ⇄ DeepSeek Harness 会话互转

把两个客户端的会话互相搬运，双向都可用，纯 Python，无需第三方服务。

| 方向 | 命令 | 产物 |
| --- | --- | --- |
| ZCode → DeepSeek Harness | `zcode2dsh` | 一个 v4 `session.v4.jsonl.zstd`，放进 DSH 的会话目录，打开就能接着写 |
| DeepSeek Harness → ZCode | `dsh2zcode` | 写进 ZCode 的 `db.sqlite`，打开 ZCode 就能接着写 |

两边都**默认只预演不落地**：不加 `--commit` 不会改任何东西。

---

## 一、在任何电脑上使用

### 需要什么

- **Python 3.9+**（Windows 上 `python`，macOS / Linux 上 `python3`）
- **Node.js**（仅 `--harness-check` / `selftest` 用得到，可省略）
- **zstandard**：`pip install zstandard`
- 目标客户端已安装过一次（ZCode 或 DeepSeek Harness），这样配置目录才存在

### 拿到代码

```bash
git clone https://github.com/BanCN-Re/ZCode-To-DeepSeek.git
cd ZCode-To-DeepSeek
pip install zstandard
```

### 先确认环境

```bash
python zcode_tool.py doctor
```

会打印 Python / Node / zstandard 版本、ZCode 数据库位置与会话数、DSH 会话目录与会话数、以及 harness 的 `app.asar` 位置。全部就绪时最后一行是 `ready`。

### 自检（不碰你的真实数据）

```bash
python zcode_tool.py selftest
```

临时目录里造一个会话，跑完整来回，再用 DeepSeek Harness 自己的读取器验一遍，然后打印 `all checks passed`。

### 路径

两个工具都会自动找默认位置，跨平台一致：

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| ZCode 数据库 | `%USERPROFILE%\.zcode\cli\db\db.sqlite` | `~/.zcode/cli/db/db.sqlite` | 同左 |
| DSH 会话目录 | `%USERPROFILE%\.dsh\sessions` | `~/.dsh/sessions` | 同左 |

装在别处就显式指定：`--db <path>`（ZCode）、`--root <dir>`（DSH）、`--asar <path>`（harness 校验用）。

---

## 二、ZCode → DeepSeek Harness

### 1. 看有哪些会话

```bash
python zcode_tool.py zcode2dsh list
```

```
SESSION                                    MESSAGES  UPDATED              TITLE
sess_14c4de89-63db-4eec-8edb-f3d3f56f1adc        16  2026-10-01 23:47:41  查看 zai-org/ZCode 项目
sess_eb9a5e0f-7eef-44b6-adfa-11f168bf6811         7  2026-10-01 22:51:39  冷咖啡 接手
```

### 2. 预演

```bash
python zcode_tool.py zcode2dsh inspect sess_eb9a5e0f-7eef-44b6-adfa-11f168bf6811
```

```
zcode session       sess_eb9a5e0f-7eef-44b6-adfa-11f168bf6811
title               冷咖啡 接手
directory           D:\Work\Fut
messages / parts    7 / 25

events to write     29
  user turns        1
  assistant steps   5
  tool calls        4 (0 errors)
...
target path         ~/.dsh/sessions/--D-Work-Fut--/sess_eb9a.../session.v4.jsonl.zstd
```

### 3. 写出

```bash
python zcode_tool.py zcode2dsh export sess_eb9a5e0f-7eef-44b6-adfa-11f168bf6811 --commit
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--cwd PATH` | 新会话挂到哪个工作区（默认沿用 ZCode 里记录的目录） |
| `--out-id ID` | 指定会话 id；不给就生成一个新的 uuid |
| `--provider` / `--model` | 写进历史里的模型标识，默认 `workbuddy` / `deepseek-v4.1-flash` |
| `--compression zstd\|none` | 默认 zstd；`none` 写成明文 `.jsonl` 便于人工查看 |
| `--drop-reasoning` | 不搬 ZCode 的思考块 |
| `--overwrite` | 目标已存在时覆盖 |
| `--commit` | 真正落盘 |

### 4. 打开

启动 DeepSeek Harness，在会话列表里按标题/时间找到它即可；命令行也可以：

```bash
dsh --profile tui --resume <session-id>
```

### 5. 校验

```bash
python zcode_tool.py zcode2dsh verify <session-id> --harness-check
```

`--harness-check` 会用 **DeepSeek Harness 自己的读取器** 把日志读回来，并重建出模型会看到的逐条消息。

---

## 三、DeepSeek Harness → ZCode

### 1. 看导出里有什么（只读）

```bash
python zcode_tool.py dsh2zcode inspect /path/to/dsh-session-xxx.zip
```

zip 或已解压目录都行。输出会区分**活跃对话**和**已被压缩淘汰**的部分：

```
successful compactions 1
   seq 4550  shadowed 1127 events
live conversation      1542 {'assistant/message': 731, 'tool/result': 754, 'user/message': 57}
live seq range         (2864, 7067)
human turns            37
tool calls / results   754 / 754 | unpaired 0
```

### 2. 预演

```bash
python zcode_tool.py dsh2zcode convert /path/to/dsh-session-xxx.zip \
    --new-session --cwd ~/Work/my-project
```

### 3. 写入

**先完全退出 ZCode**（工具检测到进程会拒绝写入，除非加 `--force`）。

```bash
python zcode_tool.py dsh2zcode convert /path/to/dsh-session-xxx.zip \
    --new-session --cwd ~/Work/my-project --commit
```

追加到已有会话而不是新建：把 `--new-session` 换成 `--session <会话id>`。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--fidelity lean\|resume\|balanced\|rich\|full` | 保留多少内容，默认 `balanced` |
| `--session ID` / `--new-session` | 追加到已有会话 / 新建 |
| `--cwd PATH` | 工作区路径 |
| `--keep-reasoning` | 搬思考文本（未签名，可能影响下一次请求） |
| `--no-history` | 不回填输入框历史 |
| `--force` | ZCode 在跑时也写（不推荐） |

### 4. 校验

```bash
python zcode_tool.py dsh2zcode verify <session-id>
```

### 5. 撤销

```bash
# 只删这次导入的行
python zcode_tool.py dsh2zcode rollback <session-id>

# 看有哪些写入前快照
python zcode_tool.py dsh2zcode rollback <session-id> --list-backups

# 从快照整段还原
python zcode_tool.py dsh2zcode rollback <session-id> --purge latest
```

每次写入前都会把整个 `db.sqlite` 快照到同级的 `_import-backup-<时间戳>/`，撤销随时可用。

---

## 四、文件

| 文件 | 作用 |
| --- | --- |
| `zcode_tool.py` | 统一入口：`dsh2zcode` / `zcode2dsh` / `doctor` / `selftest` |
| **ZCode → DSH** | |
| `dsh_import.py` | 反向转换 CLI |
| `dsh_format.py` | v4 日志的读、写、校验与路径规则 |
| `dsh_events.py` | 事件构造（含 surface 操作） |
| `dsh_from_zcode.py` | ZCode part → DSH 内容块的映射 |
| `zcode_read.py` | 只读 ZCode 数据库 |
| `dsh_check.mjs` | 用 harness 自身代码加载日志并打印转录 |
| `dsh_extract_reader.py` | 从 `app.asar` 抽出 harness 读取器模块 |
| **DSH → ZCode** | |
| `zcode_migrate.py` | 正向转换 CLI |
| `zcode_live.py` | 判定导出里哪部分仍是活跃对话 |
| `zcode_map.py` | 工具名与入参双向映射 |
| `zcode_common.py` | 定位数据库、备份、生成 id、读取原生行格式 |

---

## 五、实现上值得知道的两点

### 1. 边界不是「最后一次压缩的位置」

DSH 的会话日志是**事件流 + 覆盖记录**。真正决定模型看到什么的是
`compaction/summary` 与 `compaction/prune` 各自点名的 `shadowedSeqs`——被点名的
事件才算淘汰。仅按 seq 切分会把大量仍然活跃的内容丢掉，也会把已被 prune 的垃圾
搬过来。

`zcode_live.py` 按这个口径筛选，并且用一致性自证：活跃集合里 assistant 消息的
`tool-call` 块必须与 `tool/result` 事件**一一配对**。配平了，口径才对；不配平就
说明边界算错了。

### 2. 工具入参必须符合目标端的 schema

历史会被回放给上游，工具名不在目录里、或参数不符合该工具 schema，都会让下一步
请求失败。所以：

- 名字按双向表改写（`pwsh` ⇄ `Bash`、`job_output` ⇄ `TaskOutput`、`skill` ⇄ `Skill` …）
- 参数按目标端重写（`job_id` ⇄ `task_id`、`name` ⇄ `skill` …）
- 截断**只缩短字符串值，从不删 key**——用占位对象替换入参会直接破坏 schema

---

## 六、常见问题

**`dsh2zcode convert` 说 ZCode 正在运行**
会话库是单文件 SQLite，被占用时写入不安全。先退出 ZCode，或用前一次的快照回滚。

**`--harness-check` 说找不到 app.asar**
非默认安装位置时加 `--asar <path>`；或跳过这个开关，工具自带的校验已经覆盖同样的结构约束。

**导出的 DSH 会话在列表里看不到**
确认 `--root` 指向 DSH 真正的 `sessions` 目录（`doctor` 会告诉你默认值），且写入的是
`--<项目名>--/<会话id>/session.v4.jsonl.zstd` 这个层级。

**想先试再决定**
两个方向都支持不加 `--commit` 的预演，`zcode2dsh` 还会用 `--out-dir` 留一份
`preview.txt`。

## 许可

MIT
