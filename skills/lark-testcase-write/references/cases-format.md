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

这是格式示例，单条观察不足以覆盖 MC/DC。`not` 使用单元素数组；条件值必须为 Boolean，叶子集合必须与 conditions 一致。目标比例是 0–1，支持 requirement、item、mcdc 及覆盖项 method 名称。空分母报告 null，不当作 100%。版本可空，不补造。模板只要求其映射中实际引用的字段存在。
