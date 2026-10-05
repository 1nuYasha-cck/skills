# 核对与导出

```bash
python3 scripts/maintain.py check --config <配置> --library <库名> --out <报告>
python3 scripts/maintain.py export --config <配置> --library <库名> --out-dir <备份目录>
```

索引库（schema_profile=indexed）的 check/export 覆盖全部注册表和被引用正文，规则见 [索引库](indexed-library.md)。以下为标准库。

check 只读，报告重复编号、缺必填字段、缺分类、目录有效用例数不符、缺正文链接、业务内容与存储哈希不符。发现问题不自动修复；由 Agent 通读原文、记录和人工改动后提出修订计划。

export 只读导出三表 ndjson、正文 Markdown 和 manifest（数量、SHA-256、完整性）。分页或正文失败时保存已读取内容并标 partial，不称完整备份。输出目录已存在默认拒绝；overwrite 只允许已知导出文件目录。这里没有灾备恢复或并发条件写入能力。

重复导入重新梳理来源并 dry-run，同编号同业务 hash 为 unchanged，版本可空。内容变更 update 会再次标待审核；来源不同会提示。废弃通过计划给 `状态=已废弃`；不删除原有记录。

远端写入不是事务，前半段成功后失败会留下资源，批次会尽力保存原件链接、SHA-256与错误并回读确认；批次登记本身失败会明确报告，停止后续用例写入。脚本不盲目重试创建；检查报告及远端后再决定修复。多人并发编辑/异步延迟可能造成回读 partial，等待并核对后再继续。
