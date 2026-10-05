# skills

给 Claude Code、Codex 等 AI 编程 agent 使用的 skill 合集。每个 skill 是 `skills/` 下的一个独立目录，自包含，可以单独安装。

## Skills

| Skill                                                     | 说明                                                                                                                                 | 文档                                                               |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| [`dev-flow`](skills/dev-flow)                             | 开发全流程：项目初始化 → 技术方案 → 开发任务包 → 开发执行 → 代码评审 → 提交，用一份 `任务状态.md` 串起来，支持单 agent、多 agent 接力和 herdr 调度（调度部分调用 `herdr-scheduling`）          | [docs/dev-flow.md](docs/dev-flow.md)                             |
| [`herdr-scheduling`](skills/herdr-scheduling)             | 在 herdr 里把任务派发给另一个 pane 的 agent：调度者派发后立即结束本轮不空等，执行者完成后由后台 watcher 回调，执行者因额度限制停下时 watcher 等到恢复时间让同一会话继续；也可被其他 skill（如 `dev-flow`）调用 | [docs/herdr-scheduling.md](docs/herdr-scheduling.md)             |
| [`lark-testcase-maintain`](skills/lark-testcase-maintain) | 飞书测试用例库维护：兼容标准库与跨 Base 索引/审核台账，支持导入、审核后发布、分卷正文与索引双向链接、浏览视图隐藏技术字段、核对与导出 | [docs/lark-testcase-maintain.md](docs/lark-testcase-maintain.md) |
| [`lark-testcase-search`](skills/lark-testcase-search)     | 飞书测试用例只读检索：跨库召回、可配置发布范围、可读编号定位与正文章节回链/版本核验，由 agent 判断匹配度和复用建议 | [docs/lark-testcase-search.md](docs/lark-testcase-search.md) |
| [`lark-testcase-write`](skills/lark-testcase-write)       | 需求驱动的测试用例编写与覆盖核算：按模板生成 xlsx/docx/Markdown，保留来源与参考链接，可上传带导航的草稿并向可编辑草稿添加反链 | [docs/lark-testcase-write.md](docs/lark-testcase-write.md) |

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

三个 `lark-testcase-*` skill 可以单独安装、互不依赖。Maintain、Search 和 Write 的上传功能通过飞书命令行工具 `lark-cli` 读写飞书，需要先安装并以用户身份登录；Write 本地编写和生成文档无需登录飞书。

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

## 测试用例工作流

三个 skill 都由当前 agent 理解内容、分类并判断复用；脚本负责飞书交互、追溯与机械核验。Write 需要你明确指定需求来源，产物仍是普通文档，可独立交给 Maintain；Search 报告是可选参考。

旧 `config_version: "2.0"` 标准库配置保持兼容。已有“正式索引 + 正文分卷 + 维护台账”可选 `schema_profile: "indexed"`，通过分表 Base 与字段映射适配；Maintain 采用“导入待审核 → 人工审核 → publish”，Search 可配置默认只查已发布。日常浏览使用可读编号，长ID等技术字段保留在数据与追溯信息中；正文和索引通过双向链接导航。

本地回归与通用性检查覆盖协议和错误恢复；真实飞书写入、权限、并发及文档视觉效果需在目标环境验收。

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
