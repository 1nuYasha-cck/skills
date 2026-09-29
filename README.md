# skills

给 Claude Code、Codex 等 AI 编程 agent 使用的 skill 合集。每个 skill 是 `skills/` 下的一个独立目录，自包含，可以单独安装。

## Skills

| Skill | 说明 | 文档 |
| --- | --- | --- |
| [`dev-flow`](skills/dev-flow) | 开发全流程：项目初始化 → 技术方案 → 开发任务包 → 开发执行 → 代码评审 → 提交，用一份 `任务状态.md` 串起来，支持单 agent、多 agent 接力和 herdr 调度 | [docs/dev-flow.md](docs/dev-flow.md) |

## 安装

安装的是 `skills/<name>/` 这一层目录（内含 `SKILL.md`），仓库根的 README、文档和许可证不需要安装。

### 推荐：Skills CLI

```bash
# 先列出仓库里有哪些 skill
npx skills add 1nuYasha-cck/skills --list

# 安装到用户级（对所有项目生效）
npx skills add 1nuYasha-cck/skills --skill dev-flow -g

# 之后检查并更新
npx skills check
npx skills update
```

不加 `-g` 时只装到当前项目；用 `-a claude-code`、`-a codex` 可指定安装到哪个 agent。安装后新开会话生效；已经在运行的会话需要重新读取 skill 或新开会话。

### 手动安装

适合想自己修改 skill、或不想用 CLI 的情况：

```bash
git clone https://github.com/1nuYasha-cck/skills.git ~/src/skills

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/src/skills/skills/dev-flow ~/.claude/skills/dev-flow

# Codex
mkdir -p ~/.codex/skills
ln -s ~/src/skills/skills/dev-flow ~/.codex/skills/dev-flow
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
