# Agent 结果格式

结果由 Agent 写入，脚本只验证形状和渲染。必需键 `query`、`interpretation` 为非空字符串，`scope` 为对象，`libraries`、`cases` 为数组。允许空候选，不能省略未完成范围。

```json
{
  "query": "用户需求原文",
  "interpretation": "精确线索、展开关键词及理由",
  "scope": {
    "complete": false,
    "libraries": ["示例库"],
    "tables": ["示例表"],
    "documents": [],
    "keywords": ["示例关键词"],
    "failures": [],
    "unfinished": ["候选正文尚未读取"]
  },
  "libraries": [
    {"name": "示例库/分类", "match": "中", "reason": "根据已读目录判断", "locator": "原始链接"}
  ],
  "cases": [
    {
      "title": "示例候选",
      "display_id": "示例编号",
      "case_id": "示例系统ID",
      "library": "示例库",
      "locator": "记录或正文链接与位置",
      "match": "中",
      "reuse": "未判定",
      "reason": "索引显示相关功能，尚未读取判据",
      "differences": ["缺少目标环境证据"],
      "evidence": [{"quote": "已读原文片段", "source": "原始链接与位置"}],
      "undetermined_reason": "需要正文判据与环境"
    }
  ]
}
```

`scope.complete` 是布尔值；其余六项必须是数组，记录实际库/表/文档、关键词、失败与未完成范围。可增加每个范围的字段、页数、已读行、过滤条件等详情。库级 `match` 枚举为高/中/低/不相关/未判定，用例级另有完全匹配。`reuse` 为直接复用/修改后复用/仅参考/不建议/未判定。每条库结果必须有 name/match/reason，每条用例必须有 title/library/locator/match/reuse/reason/differences。

用例可选 `display_id`（原用例编号，与 title 组成可读标签）及追溯字段 `case_id`、`record_id`、`version`、`body_locator`；给出时须为非空文本（`version` 也可为整数）。主表和差异小节只显示可读标签，追溯字段单列在报告末尾「技术追溯」表；`locator` 仍应保留真实记录或正文链接。

用例 `evidence` 是 `{quote, source}` 数组，内容非空且来自已读原文。无证据时仅允许未判定并提供非空 `undetermined_reason`；任一判断为未判定也必须解释缺失信息。可附全部原始字段和审核状态，不凭空推断。脚本不验证引用真实性、不改变 Agent 等级；评审需人工对照原文。报告按检索理解、范围与完整性、库级表、用例按等级分组、差异与存疑、未完成范围呈现。
