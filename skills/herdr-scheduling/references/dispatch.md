# 派发入口：调度者把任务交给另一个 pane 的 agent

当前 agent 作为**调度者**时使用。herdr 命令的语法和安全规则以已安装的官方 herdr skill 和 `herdr --help` 为准。其他 skill 要求派发并守护时，选执行者之后的步骤见 [integration.md](integration.md)。

`<skill 目录>` 指本 skill 的 `SKILL.md` 所在目录的绝对路径；脚本为 `<skill 目录>/scripts/start-watcher.sh`、`quota-watcher.sh`、`wait-settled.sh`。

核心做法：派发后只确认对方**已开始**，随即结束本轮，不等待、不轮询；完成和额度中断都由 watcher 负责，调度者空闲期间不消耗额度。

## 1. 前置检查

1. `test "${HERDR_ENV:-}" = 1`，且 `$HERDR_PANE_ID`、`$HERDR_WORKSPACE_ID` 非空；否则说明不在 herdr 中并停止。
2. **分工必须明确**。同时具备下面三项才继续，缺任何一项就**停下询问**，不要猜：
   - 执行者的 kind（如 `claude`、`codex`）；
   - 要执行的任务内容；
   - 当前 agent 被明确要求担任调度者（用户说「你来调度」「由你派发」之类，或明确给出调度者和执行者）。
3. 方案 D 开关：只有用户**同时**明确说了「额度耗尽时不等待」和「改派到哪个 kind」才设 `mode=D` 并记下 `d_kind`；否则一律 `mode=A`，不要自行启用 D。

执行者可以和调度者是同一种 kind，但**永远是另一个 pane**，不在调度者本会话里执行。

## 2. 选择执行者 pane（只在当前工作区内）

执行 `herdr agent list`（它返回所有工作区的 agent），逐项过滤，全部满足才算候选：

1. `workspace_id` == `$HERDR_WORKSPACE_ID`；
2. `agent` == 执行者 kind；
3. `pane_id` != `$HERDR_PANE_ID`（排除调度者自己）；
4. `cwd` 等于任务工作目录（默认调度者当前目录）；
5. `agent_status` 为 `idle` 或 `done`。

- 候选唯一：复用，以其 `pane_id` 寻址。
- 候选多个：列出 `pane_id`、`tab_id`、`cwd`、`agent_status`，让用户选。
- 没有候选：在当前工作区新建：

  ```bash
  herdr pane split --pane "$HERDR_PANE_ID" --direction down --cwd <工作目录> --no-focus
  herdr agent start <名称，如 hs-claude> --kind <kind> --pane <新 pane>
  ```

  从返回 JSON 读 `pane_id`，并核对其 `workspace_id` == `$HERDR_WORKSPACE_ID`。

禁止：选其他工作区的 agent；新建 workspace、tab、worktree；使用 `--machine`；关闭自己未创建的 pane。

**方案 D 时**再选改派目标：同样的过滤条件（另排除执行者），**只找已有的**，不新建。找不到则**停止并报告，不派发**，也不退回等待。

## 3. 派发并启动 watcher（一条命令）

```bash
sh '<skill 目录>/scripts/start-watcher.sh' dispatch --executor <执行者 pane> \
  --scheduler "$HERDR_PANE_ID" --scheduler-kind <调度者 kind> \
  --prompt '<任务内容>'            # 长提示词用 --prompt-file <文件>，两者只能给一个
```

脚本按这个顺序做事（**先派发并确认执行者已开始，再启动 watcher**）：

1. 校验参数，在 `${TMPDIR:-/tmp}/herdr-scheduling/<任务ID>/` 下建任务目录并写 `ticket`（不写入使用者的项目）；
2. 组装完整提示词，写入 `prompt.txt`（保存的就是实际发出的内容）；
3. `herdr agent prompt <执行者> <提示词> --wait --until working --until blocked --timeout 15000`，再读一次状态确认；
   （`herdr agent prompt` 成功返回就表示已观察到执行者开始；之后它即使很快做完、读到 `idle`，也算已开始，照常进入第 4 步。）
4. 从执行者 pane 拆出一个新 pane，**核对新 pane 的工作区和 tab 与执行者一致**（不一致就关闭这个新 pane，退出码 5），在里面运行 `quota-watcher.sh`，读屏确认出现「开始守护」。**这里不带 `--require-working`**：第 3 步已经确认过执行者开始，它随后很快停下（短任务已完成，或刚开始就触达额度）时，watcher 仍必须启动——已有 `done` 它会立即回调，有额度文案它会去恢复。

成功时输出五行 `键=值`：`task_id`、`task_dir`、`result_file`、`done_file`、`watcher_pane`。

### 选项

| 选项 | 作用 |
| --- | --- |
| `--executor`、`--scheduler`、`--scheduler-kind` | 必填；`--kind` 可省（从 `herdr agent get` 读取执行者 kind）。执行者不能是调度者，也不能是当前 pane（违反返回退出码 2，不发送）；方案 D 的 `--d-pane` 不能是执行者、调度者或当前 pane |
| `--prompt` / `--prompt-file` | 任务内容，恰好给一个；不能为空，也不能以 `-` 开头 |
| `--callback-on done\|stop` | 何时回调调度者，缺省 `done`，见下 |
| `--callback-prompt <文本>` | 回调时发给调度者的一行文本，缺省「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」；不能以 `-` 开头，最长 500 字符 |
| `--result-file <路径>` | 结果文件路径，缺省为任务目录下的 `result.md` |
| `--mode D --d-pane <pane> --d-kind <kind>` | 方案 D：额度中断时改派给这个已有 pane |
| `--env QW_X=N` | 给 watcher 传环境变量（可重复，只接受 `QW_` 开头的数字参数） |

`dispatch` 不接受 `--require-working`（它已经确认过开始）。

### 两种回调条件

- **`--callback-on done`（缺省）**：脚本在提示词后追加「把结果写到 <result_file>；创建文件 <done_file>；不需要自己通知调度者，由 watcher 负责」，watcher 发现 `done` 文件才回调。适合没有别的交接手段的任务。
- **`--callback-on stop`**：提示词原样发送，不追加样板；执行者稳定停在 `idle`/`done` 且不是额度原因，watcher 就回调调度者，由调度者读调用方自己的状态文件判断结果。适合执行者靠状态文件交接的流程（见 integration.md）。执行者停在 `blocked`、`unknown` 等状态时**不回调**，见 quota-recovery.md 第 2 节。

### 退出码

| 退出码 | 含义与处理 |
| --- | --- |
| 0 | 成功，继续第 4 节 |
| 2 | 参数错误（含提示词为空、选项值不合法、选项对 `dispatch` 无效） |
| 3 | 不在 herdr 中 |
| 4 | 执行者不存在，或不在当前工作区 |
| 5 | 拆 pane 失败，或新 pane 的工作区、tab 不一致（已关闭新 pane）。**提示词已送达、执行者正在工作，但 watcher 没有启动**；任务目录保留，排除问题后用 `run <任务目录>` 重试（第 7 节），不要重发提示词 |
| 7 | watcher 没有确认启动（pane 保留，供查看）；同样提示词已送达，读 pane 找原因，或用 `run` 重试 |
| 8 | 执行者被阻塞（等批准或回答）：目标本来就是 `blocked` 时提示词没有送达，送达后才进入 `blocked` 时已送达；**没有启动 watcher**。读执行者最多 40 行交给用户，不要自己批准 |
| 9 | 没有确认执行者开始工作（`herdr agent prompt` 返回 `agent_prompt_stalled`、超时等错误，且随后读到的状态不是 `working`）：提示词可能已送达，**没有启动 watcher，不要盲目重发**；`agent read` 看一眼屏幕再报告用户 |

退出码 5、7、8、9 时任务目录都保留，输出里有 `task_dir`。

## 4. 结束本轮

向用户报告：「已派发给 <pane>，watcher 在 <pane> 中守护，完成后会回调；任务ID <ID>」，然后结束本轮并进入空闲。

## 5. 被回调唤醒后

调度者收到的就是 `ticket` 里的回调句：缺省是「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」；调用方指定过 `--callback-prompt` 时是调用方的句子，那时由调用方 skill 接手，不再按本节。

收到缺省回调句时：

1. 读 `<任务目录>/ticket`、`plan`、`result.md`（以它们为准，不读执行者的完整输出）。
2. 向用户报告结果。`plan` 的 `state`、`attempts` 可说明期间是否发生过额度中断和恢复。
3. 不要自动派发下一个任务；需要继续时等用户指示。

## 6. 手动唤醒

watcher 异常退出时，`done` 仍在。用户可以对调度者说「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」，按第 5 节处理。

## 7. 分步写法（排错或需要特殊处理时）

`dispatch` 等于下面三步，出问题时可以单独执行其中一步。

### 7.1 `init`：只建任务目录和 ticket

```bash
sh '<skill 目录>/scripts/start-watcher.sh' init --mode A --executor <执行者 pane> \
  --scheduler "$HERDR_PANE_ID" --scheduler-kind <调度者 kind> [--prompt-file <完整提示词文件>] \
  [--callback-on stop] [--callback-prompt '<文本>'] [--result-file <路径>]
```

- 不创建 pane、不启动任何进程；方案 D 改为 `--mode D --d-pane <pane> --d-kind <kind>`，`--prompt-file` 必须是**完整提示词**（改派时原样发给新执行者）。
- 输出四行 `键=值`：`task_id`、`task_dir`、`result_file`、`done_file`。
- 任务ID = `hs-<YYYYMMDDHHMMSS>-<执行者 pane，: 和 / 换成 ->`，同一秒重复时自动追加序号。
- 退出码：2 参数错误；3 不在 herdr 中；4 执行者不存在或不在当前工作区。

### 7.2 手工派发

`--callback-on done` 时，发给执行者的提示词自包含（没有状态文件可读），在任务内容后追加：

```text
完成后请：
1. 把结果写到 <result_file>
2. 创建文件 <done_file>
不需要自己通知调度者，由 watcher 负责。
```

```bash
herdr agent prompt <执行者 pane> "<提示词>" --wait --until working --until blocked --timeout 15000
```

`--until working` 只确认对方已开始，最多 15 秒，**不等待任务完成**；之后不要运行 `wait-settled.sh`，不要 `agent read` / `agent get` 轮询。返回 `blocked`：读对方最多 40 行交给用户，**不启动 watcher**。超时：`agent get` 一次确认状态后报告用户，不盲目重发。

### 7.3 `run`：启动 watcher

```bash
sh '<skill 目录>/scripts/start-watcher.sh' run "<task_dir>"
```

**已经确认过执行者开始，不要加 `--require-working`**（理由同第 3 节第 4 步）。`--require-working` 只用于「还没有确认过执行者已开始」的场景：执行者不是 `working` 时退出码 6、不创建 pane；若你已经确认过它开始，说明它很快停下了，去掉 `--require-working` 重新运行，由 watcher 判定。

`run` 只接受 `--require-working` 和 `--env`；写进 `ticket` 的选项要在 `init`（或 `watch`、`dispatch`）时指定，给 `run` 会被拒绝（退出码 2）。

| 退出码 | 含义与处理 |
| --- | --- |
| 0 | 成功 |
| 2 | 参数错误或任务目录里没有 `ticket` |
| 3 | 不在 herdr 中 |
| 4 | 执行者不存在，或不在当前工作区 |
| 5 | 拆 pane 失败，或新 pane 的工作区、tab 不一致（已关闭新 pane）；报告用户 |
| 6 | 只有加了 `--require-working` 才会出现：执行者当前不是 `working` |
| 7 | watcher 没有确认启动（pane 保留，供查看）；读 pane 找原因，或报告用户 |

脚本读 `ticket` 找到执行者，从执行者 pane 拆出新 pane 并核对工作区和 tab，在新 pane 里运行 `quota-watcher.sh`，读屏确认出现「开始守护」才算成功。

### 7.4 手工等价写法（脚本不可用时）

`herdr pane split --pane <执行者 pane> --direction down --no-focus --cwd <工作目录>`，核对新 pane 的 `workspace_id`、`tab_id` 与执行者一致，然后

```bash
herdr pane run <watcher pane> "sh '<skill 目录>/scripts/quota-watcher.sh' --ticket '<任务目录>'"
```

注意：`herdr pane run` 会把各参数**直接用空格拼接**后交给 pane 里的 shell，不保留引号，所以要把整条命令写成**一个带引号的字符串**，路径用单引号包住（因此路径里不能有单引号）；并显式用 `sh` 执行，不依赖脚本的可执行位。启动后用 `herdr pane read <watcher pane> --source visible --lines 10` 确认出现「开始守护」。这个 pane 是普通终端，不是 agent，不消耗模型额度。

### 7.5 `ticket` 格式

每行 `键=值` 的文本（脚本不可用时可手工写出等价内容，并另写 `prompt.txt`）：

```text
id=<任务ID>
mode=A|D|watch
scheduler_pane=<$HERDR_PANE_ID>      # watch 模式为空
scheduler_kind=<调度者 kind>
executor_pane=<执行者 pane>
executor_kind=<执行者 kind>
workspace_id=<$HERDR_WORKSPACE_ID>
tab_id=<执行者 pane 的 tab>
d_kind=<kind>          # 仅 mode=D
d_pane=<pane>          # 仅 mode=D
result_file=<任务目录>/result.md     # 或调度者指定的路径
done_file=<任务目录>/done
notify_stop=1          # 可选；0 表示监视模式下目标正常停下时不弹通知，默认 1
callback_on=done|stop  # 可选，仅 mode=A|D；缺省 done
callback_prompt=<一行文本>  # 可选，仅 mode=A|D；缺省「使用 $herdr-scheduling 继续调度（任务：<ID>）」
created=<Unix 秒>
```

## 规则

- 调度者不执行被派发的工作，也不修改执行者的产物，只写任务目录里的 `ticket`、`prompt.txt`。
- 调度者和执行者（agent）都不做阻塞等待；等待由 watcher 这个无 token 的脚本承担。
- 调度范围限定在调度者所在的 herdr 工作区。
- 一次只派发一个执行者；首版不支持并行。
