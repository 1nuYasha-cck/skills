# 模板结构与映射

`inspect-template` 只描述结构，不猜字段。XLSX 输出各 Sheet 尺寸、前30行值和坐标、合并、列宽行高、冻结、数据验证和公式位置；DOCX/HTML/MD 输出带定位内容。

## XLSX

```json
{"sheet":"测试用例","start_row":2,"style_row":2,"row_mode":"per_case",
 "columns":{"A":"用例编号","B":"需求编号","C":"用例标题","D":"前置条件","E":"测试步骤","F":"预期结果"},
 "clear_example_rows":[2,2],"cells":{},"list_join":"\n","number_steps":true}
```

`columns` 为 Excel 列字母到用例字段名。`start_row` 和 `style_row` 是一基行号。清示例行必须明确给 `[first,last]`，不能跨部分合并区域；不默认删除模板后续内容。`cells` 和 document.header_values 写明确坐标，cells 优先。填充行复制 style_row 样式及行高；其他 Sheet 保留。文本超过单元格上限报错，不截断；以 = 开头的测试文字保持文本。

填充前检查全部目标单元格：`clear_example_rows` 之外的非空单元格或相交合并区域会阻断并报告坐标，不生成文件。明确清除范围内的完整合并区域可解除后填写；部分交叉合并仍拒绝。固定 `cells` 是显式替换授权，但只能写合并区域锚点。

格式可通过纯机械映射声明：`"column_widths":{"A":18,"D":60,"E":60},"alignment":{"wrap_text":true,"vertical":"top","horizontal":"left"},"row_height":120`。列宽以 Excel 字符宽度、行高以点为单位，均须正有限数。alignment 使用 openpyxl Alignment 属性，覆盖填充单元格和固定 cells 的对应属性，其余样式保留。没有模板的空白工作簿默认换行和顶端对齐；列宽、行高按 Agent 明确给出的值设置，不根据内容推断。多行内容仍需 Agent 根据模板选择合适的宽高并视觉核对。

工作表名称可显式指定：`"sheet":"Sheet3","output_sheet":"功能用例"` 在输出副本中重命名该表；`"sheet":"新用例表","create_sheet":true` 在副本中新建空白工作表，保留原有所有表。create_sheet 与 output_sheet 不同时使用，目标重名或非法名称会拒绝，不自动追加数字；仅大小写变动的重命名也拒绝。新建表默认换行/顶端对齐，格式仍按映射指定。重命名不会改写其他表的公式文字，Agent 应核对引用原表名的公式，涉及引用时可选择新建表。模板原件始终不变。

两个内置映射声明列宽，编号列为30，支持常见长编号。CLI fill 只返回计数摘要及 result_out 路径；完整合并清单和SHA-256在输出同目录的 `<文件名>.result.json` 中。

`per_step` 每步骤一行，要求测试步骤和预期结果等长数组；`merge_case_columns` 指定用例级列合并，不得合并步骤/预期列。内置标准用例表沿用十一列，默认填 A:F，执行字段 G:K 留空；步骤展开表提供另一版式。

HTML 伪装模板不会直接当 XLSX 加载。Agent 阅读其结构后可使用 `fill --format xlsx`，用新建空白工作簿为底并明确 sheet/cells/columns；或用 `--format md` 渲染。不得修改或转换覆盖模板原件。

## DOCX 与 Markdown

DOCX 表格映射：`{"table":0,"style_row":1,"columns":{"0":"用例编号","1":"用例标题"}}`，表格、样式行与列为零基索引，克隆样式行后追加填写。段落模式使用 `{"paragraphs":[0,1]}`，选择文档原位置上连续且不跨表格的段落块，每条用例展开一份，在原位置替换原占位段落，保留前后内容顺序和文本样式。占位符 `{{字段名}}` 引用缺失字段时拒绝；跨多个 run 的占位符会明确报错，应选表格模式或使用另一个支持的模板（不得改原件）。段落/表格索引均为零基。

Markdown：`{"columns":["用例编号","用例标题","测试步骤","预期结果"],"mode":"table"}`；mode=sections 逐条小节。列表按 list_join 合并，表格文本转义管道符和换行。

table 模式依据公共 split_markdown 的限制（每块6000 UTF-8字节、80行）拆成多张表，每张重复表头，以空行分隔，保留每条用例一次。单条表格行无法放入安全块时明确拒绝，应选择 sections 或由 Agent 明确拆分字段，不截断文本。sections 的单个段落仍须满足分块限制。

## 默认与文件保护

优先本次模板 → 用户默认 → 内置标准用例表。用户默认位于环境变量 LARK_TESTCASE_WRITE_HOME 指定根的 default-template，缺省为用户配置目录 lark-testcase-write/default-template。只有用户明确要求设为默认，才传 --confirm-default 写入模板副本及映射；不修改安装目录。

所有 CLI 输出必须在每个输入文件目录之外；默认不覆盖已有文件，--overwrite 不能绕过来源保护。映射和 cases.json 也属于输入。输出应放独立的新目录。
