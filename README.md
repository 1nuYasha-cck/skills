# skills

给 Claude Code、Codex 等 AI 编程 agent 使用的 skill 合集。每个 skill 是 `skills/` 下的一个独立目录，自包含，可以单独安装。

## Skills

| Skill                                                     | 说明                                                                                                                                 | 文档                                                               |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| [`dev-flow`](skills/dev-flow)                             | 开发全流程：项目初始化 → 技术方案 → 开发任务包 → 开发执行 → 代码评审 → 提交，用一份 `任务状态.md` 串起来，支持单 agent、多 agent 接力和 herdr 调度（调度部分调用 `herdr-scheduling`）          | [docs/dev-flow.md](docs/dev-flow.md)                             |
| [`herdr-scheduling`](skills/herdr-scheduling)             | 在 herdr 里把任务派发给另一个 pane 的 agent：调度者派发后立即结束本轮不空等，执行者完成后由后台 watcher 回调，执行者因额度限制停下时 watcher 等到恢复时间让同一会话继续；也可被其他 skill（如 `dev-flow`）调用 | [docs/herdr-scheduling.md](docs/herdr-scheduling.md)             |
| [`lark-testcase-maintain`](skills/lark-testcase-maintain) | 飞书测试用例库维护：agent 理解并分类任意格式的用例文档，脚本经 `lark-cli` 导入多维表格和分类正文云文档并回读核对；支持增量重复导入、只读核对和导出，版本号可以缺失                                         | [docs/lark-testcase-maintain.md](docs/lark-testcase-maintain.md) |
| [`lark-testcase-search`](skills/lark-testcase-search)     | 飞书测试用例只读检索：精确或模糊的需求都可以，跨多个用例库和云文档召回，由 agent 判断库级、用例级匹配度和复用建议，输出可交给编写流程的报告                                                          | [docs/lark-testcase-search.md](docs/lark-testcase-search.md)     |
| [`lark-testcase-write`](skills/lark-testcase-write)       | 需求驱动的测试用例编写：按指定的需求和设计方法（边界值、等价类、MC/DC 等）编写用例并核算覆盖率，按任意模板生成 xlsx/docx/Markdown，可上传飞书草稿                                              | [docs/lark-testcase-write.md](docs/lark-testcase-write.md)       |

## 安装

安装的是 `skills/<name>/` 这一层目录（内含 `SKILL.md`），仓库根的 README、文档和许可证不需要安装。

### 推荐：Skills CLI

```bash
# 先列出仓库里有哪些 skill
npx skills add 1nuYasha-cck/skills --list

# 安装到用户级（对所有项目生效）
npx skills add 1nuYasha-cck/skills --skill dev-flow -g

# 要用 dev-flow 的 herdr 调度，需要同时安装 herdr-scheduling
npx skills add 1nuYasha-cck/skills --skill herdr-scheduling -g

# 之后更新
npx skills update
```

三个 `lark-testcase-*` skill 可以单独安装、互不依赖；它们通过飞书命令行工具 `lark-cli` 读写飞书，使用前需要先安装并以用户身份登录 `lark-cli`。

不加 `-g` 时只装到当前项目；用 `-a claude-code`、`-a codex` 可指定安装到哪个 agent。安装后新开会话生效；已经在运行的会话需要重新读取 skill 或新开会话。

### 手动安装

适合想自己修改 skill、或不想用 CLI 的情况：

```bash
git clone https://github.com/1nuYasha-cck/skills.git ~/src/skills

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/src/skills/skills/dev-flow ~/.claude/skills/dev-flow
ln -s ~/src/skills/skills/herdr-scheduling ~/.claude/skills/herdr-scheduling

# Codex
mkdir -p ~/.codex/skills
ln -s ~/src/skills/skills/dev-flow ~/.codex/skills/dev-flow
ln -s ~/src/skills/skills/herdr-scheduling ~/.codex/skills/herdr-scheduling
```

以后执行 `git -C ~/src/skills pull` 即可更新。如果 `~/.claude/skills`、`~/.codex/skills` 已经统一链接到 `~/.agents/skills`，只需链接一次；不需要更新时，把 `ln -s` 换成 `cp -R` 即可。

## 目录结构

```text
skills/
├── skills/<name>/     skill 本体（SKILL.md、references/、assets/、scripts/），可单独安装
├── docs/<name>.md     各 skill 的使用说明
├── README.md
└── LICENSE
```

## 许可证

[MIT](LICENSE)
