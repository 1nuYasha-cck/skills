# 调度模式 —— 通过 herdr 让其他 agent 执行下一步

当前 agent 作为**调度者**时使用。herdr 命令语法和安全规则以已安装的 herdr skill 为准；**等待方式以本文件为准**：不做阻塞等待，派发后结束本轮、由回调唤醒，避免调度者（尤其是 Codex）在等待期间反复被唤醒消耗额度。

下文 `<skill 目录>` 指本 `SKILL.md` 所在目录的绝对路径；回调用到的等待脚本随本 skill 提供：`<skill 目录>/scripts/wait-settled.sh`。

## 触发

- 单步：「通过 herdr 让 codex 执行下一步」「让同工作区的 claude 做代码评审」。
- 全流程：给出分工，如「开发执行由 claude，其余由 codex，调度由 herdr 负责」。

## 被派发 agent 收到的提示词

固定为一句，不附加背景、摘要或上下文：

```text
使用 $dev-flow 完成下一步动作：<阶段>（任务：<任务目录名>）
```

附任务目录名，是因为同一项目可能有多个未完成任务。被派发 agent 所需的全部信息都从 `任务状态.md` 和项目文件读取。

## 流程

1. **环境检查**：`test "${HERDR_ENV:-}" = 1`，不满足即说明不在 herdr 中并停止。
2. **定位任务**：按 `SKILL.md` 第 1–3 步确定项目和任务；任务不唯一时先问用户。
3. **写入调度信息**：把用户给出的分工写入「阶段分工」，并写 `调度方式：herdr`、`调度者：<本 agent kind>（$HERDR_PANE_ID）`、`调度模式：单步|全流程`、`调度起始评审轮次：<当前评审轮次>`。未指定的阶段由调度者自己执行。
4. **选下一阶段**：读 `任务状态.md`：
   - `待执行` → 该阶段可派发；
   - `待用户确认` / `阻塞`，且该阶段的待确认事项已全部标 `已确认` → 可派发（执行者按 SKILL.md 第 5 步继续）；
   - `待用户确认` / `阻塞`，仍有未确认事项 → 不派发，转第 8 步；
   - `完成` → 报告任务已完成。
5. **选目标 agent**（只在当前工作区内选择）：
   - 分工指向调度者自己的 kind → 直接在本会话按 `SKILL.md` 执行该阶段，然后回到第 4 步。
   - 否则执行 `herdr agent list`（它返回**所有工作区**的 agent），按以下条件逐项过滤 JSON，全部满足才算候选：
     1. `workspace_id` == `$HERDR_WORKSPACE_ID`；
     2. `agent`（kind）== 分工指定的 kind；
     3. `pane_id` != `$HERDR_PANE_ID`（排除调度者自己）；
     4. `cwd` 等于项目根或位于项目根之下（被派发 agent 靠当前目录定位项目，提示词里只有任务目录名）。
   - 候选唯一 → 以其 `pane_id` 作为 target（不依赖 agent 名称；无名 agent 也可用 pane ID 寻址）。
   - 候选多个 → 列出 `pane_id`、`tab_id`、`cwd`、`agent_status` 问用户选择。
   - 无候选 → 只在当前工作区内新建：`herdr pane split --current --direction <right|down> --cwd <项目根> --no-focus`，再 `herdr agent start <name> --kind <kind> --pane <新 pane>`（名称如 `devflow-codex`）；从返回 JSON 读取新 `pane_id`，并核对其 `workspace_id` == `$HERDR_WORKSPACE_ID`。
   - 禁止：选择其他工作区的 agent；新建 workspace / tab / worktree；使用 `--machine` 跨机器；把 pane 移出当前工作区。用户明确要求时例外。
   - 派发前再次 `herdr agent get <pane_id>` 核对 `workspace_id` 仍为当前工作区、状态为 `idle` / `done`；为 `blocked` 时不派发，读 40 行交给用户；为 `working` 时不插话，报告用户。
   - 本次调度确定的 target `pane_id` 记入 `任务状态.md`「阶段分工」表该阶段的 agent 列（如 `codex（wF:p1）`），后续阶段优先复用，复用前仍按上述条件复核。

6. **派发后立即结束本轮（不等待）**：

   ```bash
   herdr agent prompt <target> "使用 \$dev-flow 完成下一步动作：<阶段>（任务：<任务目录名>）" --wait --until working --until blocked --timeout 15000
   ```

   - `--until working` 只确认对方已开始处理（最多 15 秒，一次调用），**不等待阶段完成**；不运行 `wait-settled.sh`，不 `agent read` / `agent get` 轮询。
   - 返回 `blocked`：读对方最多 40 行交给用户。返回 `agent_prompt_stalled` / `timeout`：`agent get` 一次确认状态后报告用户，不盲目重发。
   - 确认开始后，在 `任务状态.md` 写 `派发记录`（阶段 → pane，时间），向用户报告「已派发 <阶段> 给 <pane>，完成后它会回调」，然后结束本轮，进入空闲。
   - 调度者空闲期间不消耗 token；被派发 agent 完成后会通过回调唤醒调度者（见下节「回调」）。
   - 命令报错（`agent_blocked`、`agent_not_found` 等）：不重试，报告用户。
7. **被回调唤醒后**：收到「使用 $dev-flow 继续调度（任务：<任务目录名>）」时，读 `任务状态.md`（以它为准，不读对方完整输出）：
   - 状态已按预期推进 → 转第 8 步。
   - 状态与派发前相同或不符合预期 → 读对方 pane 最多 80 行确认原因，报告用户，不重复派发。
8. **决定继续或停止**：
   - `调度模式：单步` → 报告本阶段结果与下一步后结束。
   - `调度模式：全流程`：新阶段为 `待执行`（或待确认事项已全部确认）→ 回到第 4 步；`待用户确认` / `阻塞` → 把「待确认事项」中未确认的问题原样转述给用户并停止。
   - 修复循环上限：`评审轮次` − `调度起始评审轮次` ≥ 3 且仍为「开发执行（修复）」时停止报告。
9. **用户答复后**：调度者把用户原话写入对应待确认事项并标 `已确认`（技术方案确认、提交确认必须来自用户明确答复），然后回到第 4 步，仍只发送标准提示词。
10. **手动唤醒**：被派发 agent 卡在审批界面或回调失败时，用户可直接对调度者说「使用 $dev-flow 继续调度」，按第 7 步处理。

## 回调（由被派发 agent 执行）

被派发 agent 按 `SKILL.md` 第 7 步写完状态后执行，以唤醒调度者。条件：`HERDR_ENV=1`，`任务状态.md` 中 `调度方式：herdr`，且 `调度者` 的 pane 不是 `$HERDR_PANE_ID`。

1. `herdr agent get <调度者 pane>`，核对 `workspace_id` == `$HERDR_WORKSPACE_ID`，不一致则不回调并在报告中说明。
2. 按调度者状态处理：
   - `idle` / `done` → 直接发送，不加 `--wait`：

     ```bash
     herdr agent prompt <调度者 pane> "使用 \$dev-flow 继续调度（任务：<任务目录名>）"
     ```

   - `working`（调度者正在和用户对话）→ 不打断它。先确认等待脚本可执行：`test -x <skill 目录>/scripts/wait-settled.sh`（不可执行时按下面 `blocked` 的方式处理并在报告中说明）。脚本可用时，启动一个脱离当前会话的后台进程，等它空闲后再发送，然后本 agent 直接结束本轮：

     ```bash
     nohup sh -c '<skill 目录>/scripts/wait-settled.sh <调度者 pane> 7200000 >/dev/null \
       && herdr agent prompt <调度者 pane> "使用 \$dev-flow 继续调度（任务：<任务目录名>）"' >/dev/null 2>&1 &
     ```

   - `blocked` / 找不到 → 不发送，在最终报告中请用户手动对调度者说「使用 $dev-flow 继续调度」。
3. 回调只发这一句，不附带结果摘要；结果都在 `任务状态.md` 中。
4. 被派发 agent 自己**不等待**调度者的后续动作，发送后立即结束本轮。

## 规则

- 技术方案确认、提交确认在调度模式下也是必停门禁；调度者不得代替用户确认。
- 调度者不执行被派发阶段的工作，也不修改阶段产物，只写调度信息（分工、调度方式、调度者、调度模式、调度起始评审轮次、派发记录）和用户确认记录。
- 调度者和被派发 agent 都不做阻塞等待；唤醒只靠回调或用户手动唤醒。
- 调度范围限定在调度者所在的 herdr 工作区（`$HERDR_WORKSPACE_ID`）；不关闭自己未创建的 pane、tab、workspace。
- 一次只派发一个阶段；阶段之间严格串行，不并行派发同一任务的不同阶段。
