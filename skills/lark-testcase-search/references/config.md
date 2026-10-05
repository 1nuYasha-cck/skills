# 配置 2.0

配置位于使用者项目，由用户指定或项目规则声明，不随 Skill 分发。`config_version` 为 `2.0`；`libraries` 数组中 `name` 唯一，`kind` 为 `standard` 或 `external`，`base_token` 非空，`tables.cases` 指定用例表；标准库通常另有 `catalog`（分类目录）、`batches`（导入批次）及 `folders.root/originals/bodies`。Search 读取所有库，不写任何库。外部库只需 cases 表，字段含义由 Agent 读 schema 判断。

`doc_scopes` 为 `{name, folder_token}` 数组，库级列表读取文件夹直接子项，子文件夹不会隐式递归。文档全文搜索使用当前用户可见范围，不假定结果均位于这些文件夹。文件夹限制由 Agent 用实际定位核对，无法确认的范围注明未知。

[示例](../assets/config.example.json) 的 token 都是假值，须由使用者替换。不要在配置保存凭据。配置目录和输出目录须分开，所有输出放在输入文件目录之外。

库对象可选 `url`：实际 Base 的完整 HTTP(S) 地址，可使用飞书租户域名或 Lark 域名，例如 `https://example.larksuite.com/base/example_base`。不要在 URL 放凭据。find 在该地址上保留已有查询项并替换 table/record，返回拼接的记录定位链接；是否可打开取决于实际地址和权限。有 url 时仍返回定位三元组；无 url 时 `url=null`，只返回 `locator.base_token/table_id/record_id`，不猜租户域名。
