# cases.json

字段名与标准用例库一致，不是入库前置协议。Agent 编写并人工可读；额外字段允许保留原文和执行历史。需求/覆盖项 ID、用例编号、判定 ID 各自唯一。

```json
{
  "cases_version": "2.0",
  "document": {"title": "功能测试用例", "header_values": {}},
  "requirements": [{"id": "REQ-01", "text": "条件A且B成立时执行动作", "source": "需求文档!A2"}],
  "design_requirements": {"methods": ["MC/DC"], "targets": {"requirement": 1.0, "mcdc": 1.0}},
  "coverage": {
    "items": [{"id": "ITEM-01", "method": "判定表", "requirement": "REQ-01", "description": "动作路径"}],
    "mcdc": [{"decision_id": "D1", "requirement": "REQ-01", "conditions": ["A", "B"],
      "expression": {"and": ["A", "B"]},
      "observations": [{"case": "TC-01", "values": {"A": true, "B": true}, "context": "同一初态"}]}]
  },
  "cases": [{"用例编号": "TC-01", "用例标题": "动作路径", "需求编号": ["REQ-01"],
    "设计方法": ["MC/DC", "判定表"], "覆盖项": ["ITEM-01"], "前置条件": "同一初态",
    "测试步骤": ["建立A和B"], "预期结果": ["执行动作"], "优先级": "高",
    "参考来源": {"type": "新写", "ref": "", "note": "依据REQ-01"}}]
}
```

## 可选来源与导航

`参考来源` 可以是旧字符串、旧对象 `{"type","ref","note"}`，或由字符串/对象组成的数组；`document.sources` 用同样的对象记录文档级需求来源或 Search 报告。全部可选，不是 Search/Maintain 的私有协议，普通文档仍可直接交 Maintain。

```json
{"document": {
   "sources": [{"kind": "requirement", "title": "需求规格", "url": "https://…", "section": "3.2 速度控制"}],
   "navigation": {"draft_folder_url": "https://…", "links": [{"title": "评审入口", "url": "https://…"}],
                  "editable_drafts": ["https://…/docx/…"]}},
 "cases": [{"用例编号": "TC-01", "参考来源": [
   {"kind": "reference_case", "type": "修改后复用", "case_no": "原用例编号", "system_id": "系统记录ID",
    "content_version": "内容版本", "index_url": "https://…", "body_url": "https://…#章节",
    "reason": "本次适配理由", "unconfirmed": ["待确认项"]},
   {"kind": "search_report", "path": "检索报告.md", "note": "Agent 已逐条复核"}]}]}
```

| 键 | 含义 |
| --- | --- |
| kind | requirement / reference_case / search_report，其他值原样保留 |
| type | 复用方式：直接复用、修改后复用、不采用、新写等 |
| title、url、section | 需求文档标题、链接、章节/范围 |
| case_no、system_id、content_version | 参考用例可读编号、系统ID（仅追溯）、内容版本 |
| index_url、body_url | 索引记录链接、正文章节链接 |
| reason、note、unconfirmed | 适配理由、说明、未确认项（文本或数组） |
| ref、path | 旧式引用或无法链接的定位，原样保留 |

只填写从原文、检索结果或系统回读确认的值，不补造 ID、版本或链接。`url`、`index_url`、`body_url`、导航链接必须是明确的 http(s) 链接，否则拒绝；记号、坐标或其他无法确认的引用放在 `ref`。未知键原样保留并显示为“其他字段”。requirement 缺 url/section、reference_case 缺编号/系统ID/版本/链接/理由时，产物和结果 JSON 标“未提供”，不当作错误也不推测。来源不参与覆盖率分母。

这是格式示例，单条观察不足以覆盖 MC/DC。`not` 使用单元素数组；条件值必须为 Boolean，叶子集合必须与 conditions 一致。目标比例是 0–1，支持 requirement、item、mcdc 及覆盖项 method 名称。空分母报告 null，不当作 100%。版本可空，不补造。模板只要求其映射中实际引用的字段存在。
