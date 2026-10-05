# 配置 2.0

配置位于使用者项目，由用户指定或项目规则声明，不随 Skill 分发。`config_version` 为 `2.0`；`libraries` 数组中 `name` 唯一，`kind` 为 `standard` 或 `external`，`base_token` 非空，`tables.cases` 指定用例表；标准库通常另有 `catalog`（分类目录）、`batches`（导入批次）及 `folders.root/originals/bodies`。Search 读取所有库，不写任何库。外部库只需 cases 表，字段含义由 Agent 读 schema 判断。

`doc_scopes` 为 `{name, folder_token}` 数组，库级列表读取文件夹直接子项，子文件夹不会隐式递归。文档全文搜索使用当前用户可见范围，不假定结果均位于这些文件夹。文件夹限制由 Agent 用实际定位核对，无法确认的范围注明未知。

[示例](../assets/config.example.json) 的 token 都是假值，须由使用者替换。不要在配置保存凭据。配置目录和输出目录须分开，所有输出放在输入文件目录之外。

库对象可选 `url`：实际 Base 的完整 HTTP(S) 地址，可使用飞书租户域名或 Lark 域名，例如 `https://example.larksuite.com/base/example_base`。不要在 URL 放凭据。find 在该地址上保留已有查询项并替换 table/record，返回拼接的记录定位链接；是否可打开取决于实际地址和权限。有 url 时仍返回定位三元组；无 url 时 `url=null`，只返回 `locator.base_token/table_id/record_id`，不猜租户域名。

## 可选扩展（均可省略，省略时行为不变）

- `schema_profile`：`standard`（缺省）或 `indexed`。`indexed` 表示「索引表 + 正文分卷文档」模型：索引表的规范字段名为 `系统用例ID`、`原用例编号`、`标题`、`摘要`、`关键词`、`需求编号`、`功能点`、`模块`、`项目`、`检索文本`、`内容版本`、`文档版本`、`正文链接`、`文档token`、`章节block ID`、`发布状态`、`审核状态`。`kind` 仍为 standard/external。
- `table_base_tokens`：`{表键: base_token}`，表键必须出现在 `tables`。未列出的表使用库的 `base_token`。cases、catalog 等可分布在不同 Base；记录定位使用该表实际所在 Base。库 `url` 指向的 Base 与 cases 所在 Base 不同时，不拼接记录链接（`url=null`），只给定位三元组。
- `field_maps`：`{表键: {规范字段名: 真实字段名}}`，同一表内不得把两个规范名映射到同一真实字段。未映射的名称按原样使用；`--search-field` 写规范名时也会映射。输出保留原始 `fields`，另在 `logical_fields` 给出每个语义角色的规范名、真实字段与值。
- `search_policy`：`{default_scope: "published"|"all", publication_field, published_values}`。`publication_field` 可写规范名（经 `field_maps.cases` 映射）。只影响 find/locate/dump 的默认范围；正式库建议 `published`。未配置时不筛选。

Search 只读取 cases、catalog 和各表的字段表；batches、sources、review 等维护表只在库级列出结构，不读写其记录。
