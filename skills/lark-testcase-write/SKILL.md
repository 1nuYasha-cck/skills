---
name: lark-testcase-write
description: 按用户指定的需求分析和设计方法编写或修订测试用例，核算需求、覆盖项与 MC/DC 覆盖，按指定模板生成 XLSX、DOCX 或 Markdown；可上传飞书草稿，并将普通文档交 Maintain 入库。
---

# 需求驱动测试用例编写

由当前 Agent 阅读、理解、选择设计方法、判断复用和编写用例。脚本只读取结构、按明确映射填表、核算 Agent 提供的覆盖模型以及进行飞书交互。不要把脚本成功或覆盖率当作需求理解正确、测试已执行的证明。

## 工作流

1. 确认用户指定的需求来源（本地文件、飞书链接或对话内容）、设计方法及覆盖目标、模板、可选参考、输出目录。没有需求来源时询问，不搜索设备。读取使用者项目约束；不要求存在任何特定私有工作区文件。
2. 本地来源调用 `extract`，飞书来源通过 `lark_io.fetch_doc` 或对应飞书读取 Skill 获取全文。Agent 按定位整理需求编号、条件、动作、输入输出、参数与矛盾。读取失败或 `extraction_required` 时明确缺口，不假装已读。
3. 可选调用 Search，或读取指定检索结果。逐条复核原文、需求与本次设计方法，决定直接复用、修改后复用、不采用；给出差异与理由。Search 结论只作参考，不强制先搜索。
4. Agent 按 [设计方法](references/design-methods.md) 建覆盖模型，按 [数据格式](references/cases-format.md) 编写 `cases.json`。已有用例修订需明确目标范围，保留执行历史和非目标内容，不覆盖来源文件。
5. 调用 `coverage` 核算。未达目标时补用例或说明缺失判据；不得缩小分母以声称达标。报告区分未覆盖与未执行。
6. 调用 `inspect-template`，Agent 依据实际结构确定字段映射；调用 `fill` 生成新文件。未指定模板时使用用户默认或内置模板。本次模板仅用于本次，只有明确要求设为默认才调用 `set-default-template --confirm-default`。
7. Agent 打开生成结果核对内容、步骤预期对应与版式；报告无法执行的视觉/公式实算检查。交付普通文档、`cases.json`、覆盖 JSON/Markdown、来源定位和复用理由。
8. 用户要求时调用 `upload` 上传到明确指定的草稿文件夹并回读。报告“草稿，不是入库”。需要正式入库时交 Maintain 按普通文档流程导入，无私有交接协议。

详细检查要点见 [workflow.md](references/workflow.md)，映射见 [templates.md](references/templates.md)。

## 命令

在 Skill 目录运行（依赖 `pip install -r scripts/requirements.txt`，不要自动修改全局环境）：

```bash
python3 scripts/write.py extract --input <需求文件> --out <独立输出目录>
python3 scripts/write.py inspect-template --template <模板> --out <独立目录/结构.json>
python3 scripts/write.py coverage --cases <cases.json> --out <独立目录/coverage.json>
python3 scripts/write.py fill --cases <cases.json> --out <独立目录/用例.xlsx>
python3 scripts/write.py fill --cases <cases.json> --template <模板> --mapping <映射.json> --out <独立目录/用例.xlsx>
python3 scripts/write.py set-default-template --template <模板> --mapping <映射.json> --confirm-default
python3 scripts/write.py upload --folder <用户指定文件夹> --file <生成文件> --markdown <草稿.md> --out <独立目录/上传报告.json> --dry-run
python3 scripts/write.py upload --config <配置.json> --library <库名> --folder-key drafts --file <生成文件> --out <独立目录/上传报告.json> --dry-run
```

输出必须位于所有输入文件目录之外，已有目标默认拒绝，显式 `--overwrite` 也不能覆盖输入或来源目录。飞书调用固定 `--as user`，权限失败不切换身份、不换库。写操作 `--dry-run` 只给出拟执行命令，不发请求。失败报告保留已完成上传，避免重复提交未知结果的写操作。

extract 允许预先建好的空输出目录；非空目录仍拒绝（包括 --overwrite），请换独立目录。coverage 同时生成 JSON 和可读 Markdown：需求、按方法覆盖项、MC/DC 独立影响对与缺口表格。fill 的 stdout 只含路径与计数，完整结果（合并区域、哈希等）写入 `<输出文件>.result.json`，已有结果文件也按覆盖规则检查。

配置模式按 `libraries[]` 中唯一的库名选择，必须明确提供 `--library` 和 `--folder-key`。目标键必须由用户指定、存在于该库 `folders` 中；禁止选择 `bodies`、`originals` 及指向这两个目录的别名，不自动回退目标目录。缺少草稿目录时使用用户明确指定的 `--folder`，不要写入库正文或原件目录。

表格草稿自动拆成带重复表头的小表，适配公共模块分块上限，不截断用例。单行仍超限时需选择 sections 或明确拆分字段。上传前将首个一级标题统一为 `--title`（未提供时为“测试用例草稿”，没有一级标题则补上），并在实际上传后核对云盘名称。同目录已有同名云文档时默认拒绝，只有用户明确要求替换时才传 `--replace-draft`；多个同名文档仍拒绝，需换唯一标题。报告标明 created/replaced 和 actual_title。dry-run 不读取云盘，因此只报告计划和未执行的重名检查，实际上传仍需检查。目录检查与创建不是原子操作，不保证并发同名创建安全，应串行上传并使用唯一标题。

stdout 一行 JSON 摘要；退出码 0 成功，2 参数或本地输入错误（包括 `extraction_required`，两个读取入口均返回 2），3 仅用于远端失败/部分完成。本地不支持格式仍写读取说明、摘要 `partial`，不假装已读。`coverage` 成功表示计算完成，`achieved=false` 表示目标未达到。覆盖模型完整性由 Agent 和审阅者判断。
