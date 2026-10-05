"""Indexed library profile: formal index plus maintenance ledger. Mechanical only.

Import stores sources, batches and pending review rows; it never publishes.
Publish re-reads the human review, writes and verifies body sections first,
then updates the formal index. The Agent supplies every interpretation.
"""
import hashlib
import html
import json
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
import lark_io as io
import maintain as m

PROFILE = 'indexed'
HASH_SCHEME = 'maintain-indexed-3'
REQUIRED_TABLES = ('catalog', 'cases', 'sources', 'batches', 'review')
OPTIONAL_TABLES = ('issues', 'routes')
ALL_TABLES = REQUIRED_TABLES + OPTIONAL_TABLES
SYSTEM_TYPES = {'created_at', 'updated_at', 'created_by', 'updated_by'}

# Canonical field name -> field type. Only fields this script reads or writes.
FIELDS = {
    'cases': {'系统用例ID': 'text', '原用例编号': 'text', '标题': 'text', '项目': 'text', '模块': 'text', '功能点': 'text',
              '需求编号': 'text', '适用版本': 'text', '用例类型': 'text', '优先级': 'select', '关键词': 'text', '摘要': 'text',
              '检索文本': 'text', '内容版本': 'text', '发布状态': 'select', '审核状态': 'select', '正文链接': 'text',
              '文档token': 'text', '章节block ID': 'text', '文档版本': 'text', '内容哈希': 'text', '来源ID': 'text',
              '来源文件链接': 'text', '导入批次ID': 'text', '工作表': 'text', '单元格范围': 'text', '待确认项': 'text'},
    'review': {'系统用例ID': 'text', '原用例编号': 'text', '用例标题': 'text', '项目': 'text', '模块': 'text', '功能点': 'text',
               '需求编号': 'text', '前置条件': 'text', '测试步骤': 'text', '预期结果': 'text', '软件适用版本': 'text',
               '来源文档版本': 'text', '执行记录': 'text', '结构化用例': 'text', '内容版本': 'text', '内容哈希': 'text',
               '元数据哈希': 'text', '审核状态': 'select', '审核人': 'user', '审核时间': 'datetime', '审核意见': 'text',
               '审核绑定内容哈希': 'text', '审核绑定元数据哈希': 'text', '入库状态': 'select', '正式索引链接': 'text',
               '正文链接': 'text', '文档token': 'text', '章节block ID': 'text', '提交键': 'text', '来源ID': 'text',
               '来源文件链接': 'text', '导入批次ID': 'text', '待确认项': 'text'},
    'catalog': {'分类ID': 'text', '项目': 'text', '模块': 'text', '功能点': 'text', '分类说明': 'text', '别名与同义词': 'text',
                '适用版本': 'text', '状态': 'select'},
    'sources': {'来源ID': 'text', '文件名称': 'text', '文件链接': 'text', '文件SHA256': 'text', '来源类型': 'text',
                '来源版本': 'text', '项目': 'text', '备注': 'text'},
    'batches': {'批次ID': 'text', '来源ID': 'text', '文件SHA256': 'text', '项目': 'text', '批次状态': 'select',
                '总用例数': 'number', '成功数': 'number', '失败数': 'number', '待确认数': 'number', '错误信息': 'text',
                '备注': 'text', '检查点': 'text'},
    'issues': {},
    'routes': {'Base token': 'text', 'Table ID': 'text', '启用状态': 'select', '目标记录上限': 'number'},
}
# Option vocabulary; each value is overridable through library.maintain.values.
VALUES = dict(review_pending='待审核', review_approved='审核通过', index_review_approved='审核通过',
              ingest_pending='未入库', ingest_done='已入库', staging_publication='待审核',
              batch_running='处理中', batch_done='已完成', batch_partial='部分完成', batch_failed='失败',
              category_enabled='启用', route_enabled='启用', priority_fallback='待确认',
              source_type='Agent梳理导入')
CONTENT_KEYS = ['用例标题', '前置条件', '测试步骤', '预期结果', '需求编号', '测试类型', '设计方法', '状态', '扩展字段']
META_KEYS = ['用例编号', '所属分类', '项目', '模块', '功能', '优先级', '适用版本', '来源版本', '关键词', '摘要']
# Standard plan field -> review column. Columns are authoritative over the structured JSON.
REVIEW_COLUMNS = {'用例标题': '用例标题', '前置条件': '前置条件', '测试步骤': '测试步骤', '预期结果': '预期结果',
                  '需求编号': '需求编号', '用例编号': '原用例编号', '项目': '项目', '模块': '模块', '功能': '功能点',
                  '适用版本': '软件适用版本', '来源版本': '来源文档版本'}
TECHNICAL = {'系统用例ID', '内容哈希', '元数据哈希', '章节block ID', '文档token', '来源ID', '导入批次ID', '提交键',
             '审核绑定内容哈希', '审核绑定元数据哈希', '结构化用例', '检索文本'}
CASE_EXTRA = {'系统用例ID', '待确认项'}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def plain(value):
    """Normalize a Base cell to text; single selects arrive as one-item lists."""
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return value[0] if len(value) == 1 else '、'.join(value)
        return m.json_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    return '' if value is None else str(value)


def first_url(value):
    """First URL in a cell; tolerates '[label](url)' and '[url](url)' forms."""
    match = re.search(r'https?://[^\s)\]"<>]+', plain(value))
    return match.group() if match else ''


def doc_token(value):
    match = re.search(r'/(?:docx|wiki)/([A-Za-z0-9]+)', plain(value))
    return match.group(1) if match else (plain(value) if re.fullmatch(r'[A-Za-z0-9]{8,}', plain(value)) else '')


# ---------------------------------------------------------------- configuration

class Library:
    """Logical table/field coordinates, possibly spread across several Bases."""

    def __init__(self, lib):
        self.lib, self.name = lib, lib['name']
        self.maps = lib.get('field_maps') or {}
        self.bases = lib.get('table_base_tokens') or {}
        self.options = lib.get('maintain') or {}
        self.values = dict(VALUES, **(self.options.get('values') or {}))
        policy = lib.get('search_policy') or {}
        self.publication_field = policy.get('publication_field', '发布状态')
        self.published_value = (policy.get('published_values') or ['已发布'])[0]
        volume = self.options.get('volume') or {}
        self.max_cases, self.max_chars = int(volume.get('max_cases', 30)), int(volume.get('max_chars', 60000))
        self.nav_headings = list(self.options.get('navigation_headings') or [])
        self.web_url = (self.options.get('web_url') or '').rstrip('/')

    def keys(self):
        return [k for k in ALL_TABLES if self.lib['tables'].get(k)]

    def base(self, key):
        return self.bases.get(key) or self.lib['base_token']

    def table(self, key):
        return self.lib['tables'][key]

    def real(self, key, name):
        if key == 'cases' and name == '发布状态':
            name = self.publication_field
        return self.maps.get(key, {}).get(name, name)

    def to_real(self, key, fields):
        return {self.real(key, k): v for k, v in fields.items()}

    def to_logical(self, key, fields):
        inverse = {self.real(key, k): k for k in FIELDS[key]}
        return {inverse.get(k, k): v for k, v in fields.items()}

    def locator(self, key, record_id=None):
        result = dict(table_key=key, base_token=self.base(key), table_id=self.table(key))
        if record_id:
            result['record_id'] = record_id
            if self.web_url:
                result['url'] = self.record_url(key, record_id)
        return result

    def record_url(self, key, record_id):
        return f'{self.web_url}/base/{self.base(key)}?table={self.table(key)}&record={record_id}' if self.web_url else ''

    def doc_url(self, token):
        return f'{self.web_url}/docx/{token}' if self.web_url else ''

    def browse_url(self):
        view = (self.options.get('browse') or {}).get('view')
        return f'{self.web_url}/base/{self.base("cases")}?table={self.table("cases")}&view={view}' if self.web_url and view else ''

    def catalog_url(self):
        doc = self.options.get('catalog_doc') or ''
        return doc if doc.startswith('http') else self.doc_url(doc) if doc else ''


def validate_config(lib):
    tables = lib.get('tables') or {}
    missing = [k for k in REQUIRED_TABLES if not tables.get(k)]
    if not lib.get('base_token') or missing:
        raise ValueError('indexed library requires base_token and tables: ' + ','.join(missing or ['base_token']))
    unknown = set(tables) - set(ALL_TABLES)
    if unknown:
        raise ValueError('unknown indexed table keys: ' + ','.join(sorted(unknown)))
    bases = lib.get('table_base_tokens') or {}
    if not isinstance(bases, dict) or set(bases) - set(tables) or any(not isinstance(v, str) or not v for v in bases.values()):
        raise ValueError('table_base_tokens must map configured table keys to base tokens')
    maps = lib.get('field_maps') or {}
    if not isinstance(maps, dict):
        raise ValueError('field_maps must be an object')
    for key, mapping in maps.items():
        if key not in tables or not isinstance(mapping, dict):
            raise ValueError('field_maps keys must be configured table keys with object values')
        bad = [k for k, v in mapping.items() if k not in FIELDS[key] or not isinstance(v, str) or not v]
        if bad or len(set(mapping.values())) != len(mapping):
            raise ValueError(f'field_maps.{key}: unknown canonical names or duplicate targets: {bad}')
    policy = lib.get('search_policy')
    if policy is not None:
        if not isinstance(policy, dict) or policy.get('default_scope', 'published') not in ('published', 'all') or not isinstance(policy.get('published_values', ['x']), list) or not policy.get('published_values', ['x']):
            raise ValueError('search_policy requires default_scope published/all and a nonempty published_values list')
    options = lib.get('maintain') or {}
    browse = options.get('browse') or {}
    if browse and (not isinstance(browse.get('fields'), list) or not browse.get('view')):
        raise ValueError('maintain.browse requires view and fields')
    volume = options.get('volume') or {}
    if any(not isinstance(volume.get(k, 1), int) or volume.get(k, 1) < 1 for k in ('max_cases', 'max_chars')):
        raise ValueError('maintain.volume thresholds must be positive integers')
    if options.get('web_url') and not re.fullmatch(r'https://[^/\s]+', options['web_url'].rstrip('/')):
        raise ValueError('maintain.web_url must be the tenant origin, e.g. https://<tenant-domain>')
    tokens = [lib['base_token'], *tables.values(), *bases.values(), *(lib.get('folders') or {}).values(), options.get('web_url', '')]
    if any('<' in str(v) for v in tokens):
        raise ValueError('example placeholder identifiers cannot be executed')
    return Library(lib)


# ---------------------------------------------------------------- reads

def read_schema(L, keys):
    result, errors = {}, {}
    for key in keys:
        fields = m.items(io.list_fields(L.base(key), L.table(key)), 'fields')
        found = {f.get('name', f.get('field_name')): f for f in fields if isinstance(f, dict)}
        result[key] = fields
        problems = []
        for name, kind in FIELDS[key].items():
            observed = found.get(L.real(key, name))
            if not observed:
                problems.append(f'{name} -> {L.real(key, name)}: missing')
            elif observed.get('type') != kind:
                problems.append(f'{name} -> {L.real(key, name)}: expected {kind}, found {observed.get("type")}')
            elif kind == 'select' and observed.get('multiple', False):
                problems.append(f'{name}: expected single select')
        if problems:
            errors[key] = problems
    return result, errors


def options_of(L, schema, key, name):
    for field in schema.get(key, []):
        if isinstance(field, dict) and field.get('name') == L.real(key, name):
            return [o.get('name') for o in field.get('options', []) if isinstance(o, dict)]
    return []


def require_options(L, schema, wanted):
    missing = [f'{key}.{name}={value}' for key, name, value in wanted if value not in options_of(L, schema, key, name)]
    if missing:
        raise ValueError('select options missing in schema: ' + ', '.join(missing))


def read_tables(L, keys, *, strict=True):
    rows, errors = {}, {}
    for key in keys:
        result = io.list_records(L.base(key), L.table(key))
        rows[key] = [dict(record_id=r['record_id'], fields=L.to_logical(key, r['fields']), raw=r['fields']) for r in result['records']]
        if not result['complete']:
            errors[key] = dict(locator=L.locator(key), error=result['error'], records_read=len(result['records']))
    if errors and strict:
        raise io.LarkError('incomplete_read', 'table read incomplete; no writes allowed', detail=errors)
    return rows, errors


# ---------------------------------------------------------------- hashing and review rows

def case_hashes(case):
    c = m.normalized(case)
    return sha(m.json_text({k: c[k] for k in CONTENT_KEYS})), sha(m.json_text({k: c[k] for k in META_KEYS}))


def review_case(fields):
    """Rebuild the logical case from a review row; returns (case, structured) or raises ValueError."""
    structured = json.loads(plain(fields.get('结构化用例')) or '{}')
    if not isinstance(structured, dict) or structured.get('hash_scheme') != HASH_SCHEME or not isinstance(structured.get('case'), dict):
        raise ValueError('legacy or foreign structured case; hash not reproducible')
    case = dict(structured['case'])
    for std, column in REVIEW_COLUMNS.items():
        case[std] = plain(fields.get(column))
    return m.normalized(case), structured


IDENTITY_COLUMNS = {'system_id': '系统用例ID', 'version': '内容版本', 'source_id': '来源ID', 'source_url': '来源文件链接',
                    'batch_id': '导入批次ID', 'pending': '待确认项'}


def submission_identity(fields):
    return {k: plain(fields.get(column)) for k, column in IDENTITY_COLUMNS.items()}


def submission_key(identity, content, meta, structured_hash):
    """Binds the whole publish input: identity, provenance, pending items, both hashes and the full structured JSON."""
    return sha(m.json_text(dict(identity, scheme=HASH_SCHEME, content_hash=content, meta_hash=meta, structured_hash=structured_hash)))


def publish_inputs(case, plan, source_name, source_sha):
    """Everything publish renders or writes besides the columns: full case (incl. 来源文件/来源位置), category, provenance."""
    sheet, cells = location(case.get('来源位置'))
    return dict(case=case, category=next((c for c in plan['categories'] if c['分类路径'] == case['所属分类']), {'分类路径': case['所属分类']}),
                provenance=dict(source_file=source_name, source_sha256=source_sha, location=case.get('来源位置'), sheet=sheet, range=cells))


def input_digest(structured):
    return sha(m.json_text({k: structured.get(k) for k in ('case', 'category', 'provenance')}))


def structured_digest(structured):
    return sha(m.json_text(structured))


def review_state(L, record):
    """Facts about the current human review of one row, read from Base only."""
    f = record['fields']
    state = dict(record_id=record['record_id'], system_id=plain(f.get('系统用例ID')), version=plain(f.get('内容版本')),
                 review=plain(f.get('审核状态')), ingest=plain(f.get('入库状态')), problems=[])
    try:
        case, structured = review_case(f)
        content, meta = case_hashes(case)
        state.update(case=case, structured=structured, content_hash=content, meta_hash=meta)
        if content != plain(f.get('内容哈希')):
            state['problems'].append('content changed after submission; resubmit through import')
        if meta != plain(f.get('元数据哈希')):
            state['problems'].append('metadata changed after submission; resubmit through import')
        for column, value in (('审核绑定内容哈希', content), ('审核绑定元数据哈希', meta)):
            if plain(f.get(column)) and plain(f.get(column)) != value:
                state['problems'].append(column + ' does not match current row')
        identity = submission_identity(f)
        state.update(structured_hash=structured_digest(structured), input_hash=input_digest(structured), submit_key=plain(f.get('提交键')))
        if submission_key(identity, content, meta, state['structured_hash']) != state['submit_key']:
            state['problems'].append('publish identity or provenance changed after submission (系统用例ID/内容版本/来源ID/来源文件链接/导入批次ID/待确认项/来源文件/来源位置/分类/结构化用例); resubmit through import')
        if structured.get('identity') != identity:
            state['problems'].append('structured identity differs from row columns; resubmit through import')
    except (ValueError, TypeError) as exc:
        state['problems'].append(str(exc))
    if state['review'] != L.values['review_approved']:
        state['problems'].append('review status is not approved: ' + (state['review'] or '<empty>'))
    elif not f.get('审核人') or not f.get('审核时间'):
        state['problems'].append('approved row lacks reviewer or review time')
    return state


def version_number(value):
    try:
        return int(plain(value))
    except ValueError:
        return 0


def latest_reviews(rows):
    by_id = {}
    for record in rows:
        sid = plain(record['fields'].get('系统用例ID'))
        if sid:
            by_id.setdefault(sid, []).append(record)
    return {sid: sorted(records, key=lambda r: version_number(r['fields'].get('内容版本'))) for sid, records in by_id.items()}


def location(text):
    match = re.fullmatch(r"\s*'?([^!']+)'?!([A-Z]+\d+(?::[A-Z]+\d+)?)\s*", text or '')
    return (match.group(1), match.group(2)) if match else ('', text or '')


# ---------------------------------------------------------------- import (archive + pending review)

def validate_plan(plan, categories):
    errors = []
    cases = plan.get('cases') if isinstance(plan, dict) else None
    stripped = json.loads(json.dumps(plan)) if isinstance(plan, dict) else plan
    if isinstance(cases, list):
        seen = set()
        for i, case in enumerate(stripped['cases']):
            if not isinstance(case, dict):
                continue
            sid = case.pop('系统用例ID', None)
            pending = case.pop('待确认项', None)
            if sid is not None:
                if not isinstance(sid, str) or not sid.strip():
                    errors.append(dict(path=f'cases[{i}].系统用例ID', message='nonempty string required'))
                elif sid in seen:
                    errors.append(dict(path=f'cases[{i}].系统用例ID', message='duplicate system id in plan'))
                seen.add(sid)
            if pending is not None and (not isinstance(pending, list) or not all(isinstance(x, str) for x in pending)):
                errors.append(dict(path=f'cases[{i}].待确认项', message='list of strings required'))
    allow = plan.get('allow_duplicate_display_ids', []) if isinstance(plan, dict) else []
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        errors.append(dict(path='allow_duplicate_display_ids', message='list of strings required'))
    if isinstance(stripped, dict):
        stripped.pop('allow_duplicate_display_ids', None)
        targets = stripped.get('targets')
        if isinstance(targets, dict) and targets.get('base') is not True:
            errors.append(dict(path='targets.base', message='indexed import stores review rows; base must be true (bodies are written by publish)'))
    standard = m.validate_plan(stripped, {'cases': m.CASES, 'existing_categories': categories})
    if isinstance(allow, list) and isinstance(cases, list):
        # Same original number inside one plan is allowed only when the Agent listed it explicitly.
        allowed = set(allow)
        standard = [e for e in standard if not (e['message'] == 'duplicate 用例编号' and isinstance(cases[int(e['path'][6:-1])], dict)
                                                 and cases[int(e['path'][6:-1])].get('用例编号') in allowed)]
    return errors + standard


def derived_id(source_sha, case):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'lark-testcase-maintain:' + source_sha + ':' + case['用例编号'] + ':' + case.get('来源位置', '')))


def latest_input_digest(record):
    try:
        structured = json.loads(plain(record['fields'].get('结构化用例')) or '{}')
    except ValueError:
        return None
    return input_digest(structured) if isinstance(structured, dict) and structured.get('hash_scheme') == HASH_SCHEME else None


def plan_operations(plan, source_sha, review_rows, index_rows, source_name=''):
    histories = latest_reviews(review_rows)
    display = {}
    for record in review_rows + index_rows:
        f = record['fields']
        display.setdefault(plain(f.get('原用例编号')), set()).add(plain(f.get('系统用例ID')))
    index_versions = {plain(r['fields'].get('系统用例ID')): version_number(r['fields'].get('内容版本')) for r in index_rows}
    allowed = set(plan.get('allow_duplicate_display_ids', []))
    operations, seen = [], set()
    for raw in plan['cases']:
        explicit = raw.get('系统用例ID')
        case = m.normalized({k: v for k, v in raw.items() if k not in CASE_EXTRA})
        sid = explicit or derived_id(source_sha, case)
        content, meta = case_hashes(case)
        history = histories.get(sid, [])
        latest = history[-1] if history else None
        op = dict(system_id=sid, display_id=case['用例编号'], explicit_id=bool(explicit), content_hash=content, meta_hash=meta,
                  case=case, pending=raw.get('待确认项') or [])
        op['input_hash'] = input_digest(publish_inputs(case, plan, source_name, source_sha))
        others = display.get(case['用例编号'], set()) - {sid, ''}
        if sid in seen:
            op.update(action='conflict', reason='two plan cases resolve to the same system id; give distinct 来源位置 or explicit 系统用例ID', version=0)
            op['submit_key'] = ''; operations.append(op); continue
        seen.add(sid)
        if latest is None:
            op['action'] = 'create'
            op['version'] = index_versions.get(sid, 0) + 1
            if others and case['用例编号'] not in allowed:
                op.update(action='conflict', reason='original number already used by other system ids; set 系统用例ID to revise or list it in allow_duplicate_display_ids', existing_system_ids=sorted(others))
        else:
            # Unchanged only when every publish input matches a row of the current binding scheme;
            # older rows are never treated as reviewed for the new binding.
            same = (plain(latest['fields'].get('内容哈希')) == content and plain(latest['fields'].get('元数据哈希')) == meta
                    and plain(latest['fields'].get('待确认项')) == m.json_text(op['pending']) and latest_input_digest(latest) == op['input_hash'])
            op['version'] = max(version_number(latest['fields'].get('内容版本')), index_versions.get(sid, 0)) + (0 if same else 1)
            op['action'] = 'unchanged' if same else {'update': 'revise', 'skip': 'skip', 'fail': 'conflict'}[plan.get('on_conflict', 'update')]
            op['previous_review'] = dict(record_id=latest['record_id'], version=plain(latest['fields'].get('内容版本')), review=plain(latest['fields'].get('审核状态')))
        operations.append(op)
    counts = {a: sum(o['action'] == a for o in operations) for a in ('create', 'revise', 'unchanged', 'skip', 'conflict')}
    return operations, counts


def review_row(L, op, plan, source, batch_id):
    case = op['case']
    identity = dict(system_id=op['system_id'], version=str(op['version']), source_id=source['id'], source_url=source['url'],
                    batch_id=batch_id, pending=m.json_text(op['pending']))
    structured = dict(schema_version='1.0', producer='lark-testcase-maintain', hash_scheme=HASH_SCHEME, identity=identity,
                      agent_notes=plan.get('agent_notes', []), **publish_inputs(case, plan, source['name'], source['sha']))
    op['submit_key'] = submission_key(identity, op['content_hash'], op['meta_hash'], structured_digest(structured))
    fields = {'系统用例ID': op['system_id'], '原用例编号': case['用例编号'], '用例标题': case['用例标题'], '项目': case['项目'],
              '模块': case['模块'], '功能点': case['功能'], '需求编号': case['需求编号'], '前置条件': case['前置条件'],
              '测试步骤': case['测试步骤'], '预期结果': case['预期结果'], '软件适用版本': case['适用版本'],
              '来源文档版本': case['来源版本'], '结构化用例': m.json_text(structured), '内容版本': str(op['version']),
              '内容哈希': op['content_hash'], '元数据哈希': op['meta_hash'], '审核状态': [L.values['review_pending']],
              '入库状态': [L.values['ingest_pending']], '提交键': op['submit_key'], '来源ID': source['id'],
              '来源文件链接': source['url'], '导入批次ID': batch_id, '待确认项': m.json_text(op['pending'])}
    return L.to_real('review', fields)


def run_import(lib, plan, *, dry_run=False):
    L = validate_config(lib)
    keys = list(REQUIRED_TABLES)
    report = dict(status='failed', profile=PROFILE, published=False, dry_run=dry_run, steps=[], operations=[],
                  locators={k: L.locator(k) for k in L.keys()},
                  note='Import archives the source and stores pending review rows only; saving is not publishing.')
    wrote, batch_record = False, None
    try:
        schema, mismatch = read_schema(L, keys)
        if mismatch:
            raise ValueError('indexed schema mismatch: ' + m.json_text(mismatch))
        require_options(L, schema, [('review', '审核状态', L.values['review_pending']), ('review', '入库状态', L.values['ingest_pending']),
                                    *[('batches', '批次状态', L.values[k]) for k in ('batch_running', 'batch_done', 'batch_partial', 'batch_failed')]])
        rows, _ = read_tables(L, keys)
        categories = [plain(r['fields'].get('分类ID')) for r in rows['catalog']]
        errors = validate_plan(plan, categories)
        if errors:
            raise ValueError('invalid plan: ' + m.json_text(errors))
        source_path = Path(plan['batch']['source_file'])
        raw_hash = sha256_file(source_path)
        prepared = json.loads(json.dumps(plan))
        for case in prepared['cases']:
            case.setdefault('来源文件', source_path.name)
        operations, counts = plan_operations(prepared, raw_hash, rows['review'], rows['cases'], source_path.name)
        report.update(operations=[dict(o) for o in operations], counts=counts)
        if counts['conflict']:
            report['error'] = dict(code='conflict', message='conflicts found; no writes'); return report
        existing_source = [r for r in rows['sources'] if plain(r['fields'].get('文件SHA256')) == raw_hash and first_url(r['fields'].get('文件链接'))]
        writes = [o for o in operations if o['action'] in ('create', 'revise')]
        if dry_run:
            batch_id, source = '<batch>', dict(id='<source>', url='<original-url>', name=source_path.name, sha=raw_hash)
            commands = [] if existing_source else [io.upload_file(source_path, lib['folders']['originals'], dry_run=True),
                                                   io.batch_create(L.base('sources'), L.table('sources'), [{'文件SHA256': raw_hash}], dry_run=True)]
            commands.append(io.batch_create(L.base('batches'), L.table('batches'), [{'批次ID': batch_id}], dry_run=True))
            if writes:
                commands.append(io.batch_create(L.base('review'), L.table('review'), [review_row(L, o, prepared, source, batch_id) for o in writes], dry_run=True))
            report.update(status='ok', commands=commands, steps=['upload/reuse source', 'register source', 'register batch', 'create pending review rows', 'readback'])
            return report
        batch_id = 'batch-' + uuid.uuid4().hex
        report['batch_id'] = batch_id
        if existing_source:
            record = existing_source[0]
            source = dict(id=plain(record['fields'].get('来源ID')), url=first_url(record['fields'].get('文件链接')), name=source_path.name, sha=raw_hash, reused=True)
        else:
            if not (lib.get('folders') or {}).get('originals'):
                raise ValueError('folders.originals required to archive a new source')
            wrote = True
            url = m.get_url(io.upload_file(source_path, lib['folders']['originals']), 'file')
            source = dict(id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'lark-testcase-maintain-source:' + raw_hash)), url=url, name=source_path.name, sha=raw_hash, reused=False)
            project = next((o['case']['项目'] for o in operations if o['case']['项目']), '')
            row = L.to_real('sources', {'来源ID': source['id'], '文件名称': source_path.name, '文件链接': url, '文件SHA256': raw_hash,
                                        '来源类型': L.values['source_type'], '来源版本': next((o['case']['来源版本'] for o in operations if o['case']['来源版本']), ''),
                                        '项目': project, '备注': plan['batch'].get('note', '')})
            m.require_batch(io.batch_create(L.base('sources'), L.table('sources'), [row]))
        report['source'] = dict(source, locator=L.locator('sources'))
        batch_fields = {'批次ID': batch_id, '来源ID': source['id'], '文件SHA256': raw_hash, '项目': next((o['case']['项目'] for o in operations if o['case']['项目']), ''),
                        '批次状态': [L.values['batch_running']], '总用例数': len(operations), '成功数': 0, '失败数': 0,
                        '待确认数': sum(bool(o['pending']) for o in operations), '备注': plan['batch'].get('note', ''), '检查点': 'import:registered'}
        wrote = True
        m.require_batch(io.batch_create(L.base('batches'), L.table('batches'), [L.to_real('batches', batch_fields)]))
        found = find_rows(L, 'batches', '批次ID', batch_id)
        if len(found) != 1:
            raise io.LarkError('batch_readback', 'batch registration unconfirmed; no review writes')
        batch_record = found[0]['record_id']
        report['batch_locator'] = L.locator('batches', batch_record)
        created = [review_row(L, o, prepared, source, batch_id) for o in writes]
        if created:
            m.require_batch(io.batch_create(L.base('review'), L.table('review'), created))
        after, _ = read_tables(L, ['review'])
        by_key = {}
        for record in after['review']:
            by_key.setdefault(plain(record['fields'].get('提交键')), []).append(record)
        readback = []
        for op in writes:
            found = by_key.get(op['submit_key'], [])
            if len(found) != 1:
                readback.append(dict(system_id=op['system_id'], display_id=op['display_id'], error=f'{len(found)} review rows for submit key'))
                continue
            state = review_state(L, found[0])
            if state.get('content_hash') != op['content_hash'] or state.get('meta_hash') != op['meta_hash'] or state['review'] != L.values['review_pending']:
                readback.append(dict(system_id=op['system_id'], display_id=op['display_id'], error='review row readback mismatch'))
            op_report = next(o for o in report['operations'] if o['system_id'] == op['system_id'])
            op_report['review_locator'] = L.locator('review', found[0]['record_id'])
        report['readback'] = dict(complete=True, errors=readback)
        report['status'] = 'partial' if readback else 'ok'
        report['next_steps'] = 'A human reviews rows in the review table; then run publish. Publication reads the current review, never this report.'
    except io.LarkError as exc:
        report.update(status='partial' if wrote else 'failed', error=exc.as_dict())
        if isinstance(exc.detail, dict) and exc.detail.get('unknown_outcome'):
            report['unknown_outcome'] = 'write outcome unknown; read back before any retry; nothing was retried'
    except (ValueError, OSError, TypeError) as exc:
        report.update(status='partial' if wrote else 'failed', input_error=str(exc))
    finally:
        if batch_record and not dry_run:
            finish_batch(L, batch_record, report, sum(o['action'] in ('create', 'revise') for o in report.get('operations', [])))
    return report


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_rows(L, key, field, value):
    result = io.list_records(L.base(key), L.table(key))
    if not result['complete']:
        raise io.LarkError('incomplete_read', f'{key} readback incomplete', detail=result['error'])
    return [r for r in result['records'] if plain(L.to_logical(key, r['fields']).get(field)) == value]


def finish_batch(L, record_id, report, success):
    status = {'ok': L.values['batch_done'], 'partial': L.values['batch_partial']}.get(report['status'], L.values['batch_failed'])
    error = report.get('error') or report.get('input_error') or (report.get('readback') or {}).get('errors') or ''
    failed = len((report.get('readback') or {}).get('errors') or [])
    fields = {'批次状态': [status], '成功数': max(success - failed, 0) if report['status'] != 'failed' else 0, '失败数': failed,
              '错误信息': m.json_text(error) if error else '', '检查点': 'final:' + report['status']}
    try:
        m.require_batch(io.batch_update(L.base('batches'), L.table('batches'), [dict(record_id=record_id, fields=L.to_real('batches', fields))]))
        found = [r for r in find_rows(L, 'batches', '检查点', fields['检查点']) if r['record_id'] == record_id]
        report['batch_audit'] = dict(confirmed=len(found) == 1, locator=L.locator('batches', record_id), result=status)
    except io.LarkError as exc:
        report['batch_audit'] = dict(confirmed=False, error=exc.as_dict())
    if not report['batch_audit']['confirmed'] and report['status'] == 'ok':
        report['status'] = 'partial'


# ---------------------------------------------------------------- documents

class _Blocks(HTMLParser):
    VOID = {'br', 'img', 'hr', 'input', 'col', 'meta', 'link'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.top, self.ids, self.headings, self._heading = 0, [], [], [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'title' or tag in self.VOID:
            return
        if self.depth == 0:
            self.top.append(dict(tag=tag, id=attrs.get('id')))
        if re.fullmatch(r'h[1-6]', tag) and self._heading is None:
            self._heading = dict(tag=tag, id=attrs.get('id'), text='', top=self.depth == 0)
        self.depth += 1

    def handle_startendtag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if self.depth == 0 and tag not in ('title', *self.VOID):
            self.top.append(dict(tag=tag, id=attrs.get('id')))

    def handle_endtag(self, tag):
        if tag == 'title' or tag in self.VOID:
            return
        self.depth = max(self.depth - 1, 0)
        if self._heading and tag == self._heading['tag']:
            self._heading['text'] = re.sub(r'\s+', ' ', self._heading['text']).strip()
            self.headings.append(self._heading); self._heading = None

    def handle_data(self, data):
        if self._heading is not None:
            self._heading['text'] += data


def fetch_blocks(doc):
    response = io.data(io.run_lark(['docs', '+fetch', '--doc', doc, '--detail', 'with-ids', '--doc-format', 'xml'], json_flag=False))
    document = response.get('document', response)
    content = document.get('content', '')
    parser = _Blocks(); parser.feed(content)
    return dict(content=content, revision=document.get('revision_id', document.get('revision')), ids=parser.ids,
                top=parser.top, headings=parser.headings, length=len(content))


def esc(value):
    return html.escape(plain(value), quote=True)


def link_xml(url, label):
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def heading_text(case, version):
    return f'{case["用例编号"]} {case["用例标题"]}（内容版本 {version}）'.strip()


def section_xml(case, version, links):
    nav = ' · '.join(link_xml(url, label) for label, url in links if url)
    parts = [f'<h2>{esc(heading_text(case, version))}</h2>']
    if nav:
        parts.append(f'<p>{nav}</p>')
    rows = [('所属分类', case.get('所属分类')), ('项目', case.get('项目')), ('模块', case.get('模块')), ('功能', case.get('功能')),
            ('测试类型', case.get('测试类型')), ('设计方法', case.get('设计方法')), ('需求编号', case.get('需求编号')),
            ('优先级', case.get('优先级')), ('适用版本', case.get('适用版本') or '未提供'), ('来源版本', case.get('来源版本') or '未提供'),
            ('关键词', case.get('关键词')), ('摘要', case.get('摘要')), ('状态', case.get('状态'))]
    parts += [f'<p>{esc(k)}：{esc(v)}</p>' for k, v in rows if plain(v)]
    for title, key in (('前置条件', '前置条件'), ('测试步骤', '测试步骤'), ('预期结果', '预期结果')):
        if plain(case.get(key)):
            parts.append(f'<h3>{title}</h3>')
            parts += [f'<p>{esc(line)}</p>' for line in plain(case[key]).split('\n')]
    extension = case.get('扩展字段') or {}
    if extension:
        parts.append('<h3>扩展字段</h3>')
        parts += [f'<p>{esc(k)}：{esc(v if isinstance(v, str) else m.json_text(v))}</p>' for k, v in extension.items()]
    parts.append(f'<p>来源：{esc(case.get("来源文件"))} {esc(case.get("来源位置"))}</p>')
    return ''.join(parts)


def doc_write(args, input_text, verify):
    """Send one document write. Unknown outcome is verified by readback, never retried."""
    try:
        return dict(response=io.data(io.run_lark(args, json_flag=False, input_text=input_text)), recovered=False)
    except io.LarkError as exc:
        if not io._unknown_write(exc):
            raise
        try:
            if verify():
                return dict(response=None, recovered=True)
        except io.LarkError as probe:
            exc.detail = dict(original=exc.as_dict(), readback_error=probe.as_dict())
        raise io.LarkError('document_unknown', 'document write unconfirmed; read back before another write', detail=dict(args=args[:4], error=exc.as_dict(), unknown_outcome=True)) from exc


def create_volume(L, title, header_xml, dry_run=False):
    folder = (L.lib.get('folders') or {}).get('bodies')
    if not folder:
        raise ValueError('folders.bodies required to create a new body volume')
    args = ['docs', '+create', '--title', title, '--parent-token', folder, '--content', '-']
    if dry_run:
        return io.run_lark(args, dry_run=True, json_flag=False, input_text=header_xml)
    existing = io._doc_from_files(io.list_folder(folder), title)
    if existing:
        document = existing['document']
    else:
        result = doc_write(args, header_xml, lambda: io._doc_from_files(io.list_folder(folder), title))
        document = result['response'] if result['response'] is not None else io._doc_from_files(io.list_folder(folder), title)['document']
        document = document.get('document', document)
    token = document.get('token') or document.get('document_id') or doc_token(document.get('url', ''))
    if not token:
        raise io.LarkError('response_shape', 'created document token missing', detail=document)
    return dict(token=token, url=document.get('url') or L.doc_url(token), recovered=bool(existing))


HEADING_TAG = re.compile(r'<(h[1-6])\b([^>]*)>(.*?)</\1>', re.S)


def sections(content):
    """Heading sections in document order; a section runs to the next heading of the same or higher level."""
    heads = []
    for match in HEADING_TAG.finditer(content):
        found = re.search(r'\bid="([^"]+)"', match.group(2))
        text = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', match.group(3)))).strip()
        heads.append(dict(level=int(match.group(1)[1]), id=found.group(1) if found else None, text=text, start=match.start(), end=match.end()))
    for i, head in enumerate(heads):
        stop = next((h['start'] for h in heads[i + 1:] if h['level'] <= head['level']), len(content))
        head['raw'] = content[head['end']:stop]
        head['body'] = html.unescape(head['raw'])
    return heads


def section_text(xml):
    """Comparable section text: link paragraphs (navigation) dropped, tags removed before unescaping, no whitespace."""
    xml = re.sub(r'<p\b[^>]*>(?:(?!</p>).)*?<a\b.*?</p>', '', xml, flags=re.S)
    return re.sub(r'\s+', '', html.unescape(re.sub(r'<[^>]+>', '', xml)))


def expected_text(case, version):
    xml = section_xml(case, version, [])
    return section_text(xml[xml.index('</h2>') + 5:])


def section_matches(section, case, version):
    return section_text(section['raw']) == expected_text(case, version)


VERSION_HEADING = re.compile(r'内容版本 \d+')


def case_sections(blocks):
    """Case sections actually present in a volume, including retained historical versions."""
    return sum(bool(VERSION_HEADING.search(h['text'])) for h in blocks['headings'])


def owned_sections(content, text, record_url):
    """Sections whose readable heading matches and whose body links back to exactly this index record.

    Readable headings may repeat (same original number and title from different sources);
    identity is the unique index record backlink, never the heading alone.
    """
    return [h for h in sections(content) if h['text'] == text and h['id'] and backlinks(h['body'], record_url)]


def backlinks(body, record_url):
    return bool(record_url) and re.search(re.escape(record_url) + r'(?![\w-])', body) is not None


def insert_section(L, doc, case, version, links, before, record_url):
    """Insert one case section ahead of configured navigation; verify it by heading and record backlink."""
    text = heading_text(case, version)
    owned = owned_sections(before['content'], text, record_url)
    if len(owned) == 1:
        if not section_matches(owned[0], case, version):
            raise io.LarkError('section_conflict', 'a section for this record and version exists but its content differs; not adopted, not overwritten',
                               detail=dict(doc=doc, heading=text, anchor=owned[0]['id']))
        # A previous unknown write did land for this very record, version and content.
        return dict(anchor=owned[0]['id'], recovered=True, blocks=before)
    if owned:
        raise io.LarkError('ambiguous_anchor', 'several sections link back to this record and version', detail=dict(doc=doc, heading=text, anchors=[h['id'] for h in owned]))
    nav = next((i for i, b in enumerate(before['top']) if b['tag'].startswith('h') and any(
        h['id'] == b['id'] and h['text'] in L.nav_headings for h in before['headings'])), None)
    args = ['docs', '+update', '--doc', doc, '--content', '-']
    if nav is not None and nav > 0 and before['top'][nav - 1].get('id'):
        args += ['--command', 'block_insert_after', '--block-id', before['top'][nav - 1]['id']]
    else:
        args += ['--command', 'append']
    if before.get('revision') is not None:
        args += ['--revision-id', str(before['revision'])]

    def verify():
        found = owned_sections(fetch_blocks(doc)['content'], text, record_url)
        return len(found) == 1 and section_matches(found[0], case, version)
    doc_write(args, section_xml(case, version, links), verify)
    after = fetch_blocks(doc)
    found = owned_sections(after['content'], text, record_url)
    if len(found) != 1 or found[0]['id'] in before['ids'] or not section_matches(found[0], case, version):
        raise io.LarkError('section_readback', 'inserted section not found exactly once with its record backlink', detail=dict(doc=doc, heading=text, found=len(found)))
    return dict(anchor=found[0]['id'], recovered=False, blocks=after)


# ---------------------------------------------------------------- publish

def check_routes(L, rows, new_count):
    if 'routes' not in rows:
        return []
    problems = []
    route = [r for r in rows['routes'] if plain(r['fields'].get('Table ID')) == L.table('cases') and plain(r['fields'].get('Base token')) == L.base('cases')]
    if len(route) != 1:
        problems.append('cases table is not registered exactly once in routes')
    else:
        fields = route[0]['fields']
        if plain(fields.get('启用状态')) != L.values['route_enabled']:
            problems.append('cases table route is not enabled')
        limit = fields.get('目标记录上限')
        if isinstance(limit, (int, float)) and len(rows['cases']) + new_count > limit:
            problems.append(f'route capacity exceeded: {len(rows["cases"])}+{new_count}>{limit}')
    return problems


def index_fields(L, state, links, status, schema):
    case, structured = state['case'], state['structured']
    review = state['fields']
    sheet, cells = location(case.get('来源位置'))
    pending = json.loads(plain(review.get('待确认项')) or '[]')
    priority = case.get('优先级') or L.values['priority_fallback']
    if priority not in options_of(L, schema, 'cases', '优先级'):
        pending = list(pending) + [f'优先级原值未在索引选项中：{priority}']
        priority = L.values['priority_fallback']
    search = '\n'.join(x for x in [case['用例标题'], case.get('摘要'), case.get('需求编号') and '需求：' + case['需求编号'],
                                   case.get('前置条件') and '前置：' + case['前置条件'], case.get('测试步骤') and '步骤：' + case['测试步骤'],
                                   case.get('预期结果') and '预期：' + case['预期结果']] if x)
    fields = {'系统用例ID': state['system_id'], '原用例编号': case['用例编号'], '标题': case['用例标题'], '项目': case['项目'],
              '模块': case['模块'], '功能点': case['功能'], '需求编号': case['需求编号'], '适用版本': case['适用版本'],
              '用例类型': case['测试类型'], '优先级': [priority], '关键词': case['关键词'], '摘要': case['摘要'], '检索文本': search,
              '内容版本': state['version'], '审核状态': [L.values['index_review_approved']], '发布状态': [status],
              '内容哈希': state['content_hash'], '来源ID': plain(review.get('来源ID')), '来源文件链接': plain(review.get('来源文件链接')),
              '导入批次ID': plain(review.get('导入批次ID')), '工作表': sheet, '单元格范围': cells, '待确认项': m.json_text(pending)}
    fields.update(links)
    return fields


def publish(lib, selection=None, *, dry_run=False):
    L = validate_config(lib)
    report = dict(status='failed', profile=PROFILE, dry_run=dry_run, published=[], pending_review=[], rejected=[], unchanged=[], steps=[],
                  locators={k: L.locator(k) for k in L.keys()}, missing_navigation_config=[])
    wrote, batch_record, eligible, repairs = False, None, [], []
    try:
        if not L.web_url:
            raise ValueError('maintain.web_url (tenant origin) is required to build record and section links; links are never guessed')
        if not L.browse_url():
            report['missing_navigation_config'].append('maintain.browse.view (browse entry link omitted)')
        if not L.catalog_url():
            report['missing_navigation_config'].append('maintain.catalog_doc (body catalog link omitted)')
        if selection is not None:
            if not isinstance(selection, dict) or set(selection) - {'publish_version', 'library', 'system_ids', 'note'} or selection.get('publish_version') != '1.0' or not isinstance(selection.get('system_ids'), list) or selection.get('library', L.name) != L.name:
                raise ValueError('publish plan accepts publish_version=1.0, library, system_ids and note only; review status is always read from Base')
        keys = [k for k in ('catalog', 'cases', 'review', 'batches', 'routes') if k in L.keys()]
        schema, mismatch = read_schema(L, keys)
        if mismatch:
            raise ValueError('indexed schema mismatch: ' + m.json_text(mismatch))
        require_options(L, schema, [('cases', '发布状态', L.published_value), ('cases', '发布状态', L.values['staging_publication']),
                                    ('cases', '审核状态', L.values['index_review_approved']), ('cases', '优先级', L.values['priority_fallback']),
                                    ('review', '入库状态', L.values['ingest_done']), ('catalog', '状态', L.values['category_enabled']),
                                    *[('batches', '批次状态', L.values[k]) for k in ('batch_running', 'batch_done', 'batch_partial', 'batch_failed')]])
        rows, _ = read_tables(L, keys)
        index_by_id = {}
        for record in rows['cases']:
            index_by_id.setdefault(plain(record['fields'].get('系统用例ID')), []).append(record)
        histories = latest_reviews(rows['review'])
        wanted = selection['system_ids'] if selection else [sid for sid, h in histories.items() if plain(h[-1]['fields'].get('入库状态')) != L.values['ingest_done']]
        for sid in wanted:
            history = histories.get(sid)
            if not history:
                report['rejected'].append(dict(system_id=sid, reasons=['no review row'])); continue
            if len(index_by_id.get(sid, [])) > 1:
                report['rejected'].append(dict(system_id=sid, reasons=['duplicate index rows for system id'])); continue
            latest = history[-1]
            if sum(version_number(r['fields'].get('内容版本')) == version_number(latest['fields'].get('内容版本')) for r in history) > 1:
                report['rejected'].append(dict(system_id=sid, reasons=['ambiguous latest version'])); continue
            state = dict(review_state(L, latest), fields=latest['fields'])
            current = index_by_id.get(sid, [None])[0]
            if current and plain(current['fields'].get('发布状态')) == L.published_value and plain(current['fields'].get('内容版本')) == state['version'] and plain(current['fields'].get('内容哈希')) == state.get('content_hash'):
                # Already in the formal index: never bypass the current review, then repair the ledger idempotently.
                if state['problems']:
                    report['rejected'].append(dict(system_id=sid, display_id=plain(latest['fields'].get('原用例编号')), version=state['version'], review_locator=L.locator('review', latest['record_id']),
                                                   index_locator=L.locator('cases', current['record_id']), reasons=['index already published but current review is not valid'] + state['problems'])); continue
                if state['ingest'] != L.values['ingest_done'] or plain(latest['fields'].get('章节block ID')) != plain(current['fields'].get('章节block ID')):
                    repairs.append(dict(state, index=current)); continue
                report['unchanged'].append(dict(system_id=sid, display_id=plain(latest['fields'].get('原用例编号')), version=state['version'])); continue
            if state['problems'] == ['review status is not approved: ' + L.values['review_pending']]:
                report['pending_review'].append(dict(system_id=sid, display_id=plain(latest['fields'].get('原用例编号')), version=state['version'], review_locator=L.locator('review', latest['record_id']))); continue
            if state['problems']:
                report['rejected'].append(dict(system_id=sid, display_id=plain(latest['fields'].get('原用例编号')), version=state['version'], review_locator=L.locator('review', latest['record_id']), reasons=state['problems'])); continue
            probe = [('返回用例索引（' + state['case']['用例编号'] + '）', L.record_url('cases', 'rec' + '0' * 14)), ('返回检索浏览', L.browse_url()), ('返回正文目录', L.catalog_url())]
            if len(section_xml(state['case'], state['version'], probe)) > L.max_chars:
                report['rejected'].append(dict(system_id=sid, display_id=state['case']['用例编号'], version=state['version'],
                                               reasons=['single case section exceeds maintain.volume.max_chars; a case is never split'])); continue
            state['index'] = current
            eligible.append(state)
        route_problems = check_routes(L, rows, sum(s['index'] is None for s in eligible))
        if route_problems:
            raise ValueError('route check failed: ' + '; '.join(route_problems))
        assignments = assign_volumes(L, eligible, rows['cases'])
        report['plan'] = [dict(system_id=s['system_id'], display_id=s['case']['用例编号'], version=s['version'], volume=a['label'],
                               action='revise' if s['index'] else 'new') for s, a in assignments]
        report['plan'] += [dict(system_id=s['system_id'], display_id=s['case']['用例编号'], version=s['version'], action='repair_review_ledger') for s in repairs]
        if not eligible and not repairs:
            report['status'] = 'ok'; return report
        if dry_run:
            commands = []
            for s, a in assignments:
                record = s['index']['record_id'] if s['index'] else '<staged-record>'
                links = [(f'返回用例索引（{s["case"]["用例编号"]}）', L.record_url('cases', record)), ('返回检索浏览', L.browse_url()), ('返回正文目录', L.catalog_url())]
                commands.append(io.run_lark(['docs', '+update', '--doc', a['doc'] or '<new-volume>', '--command', 'block_insert_after', '--content', '-'],
                                            dry_run=True, json_flag=False, input_text=section_xml(s['case'], s['version'], links)))
            staged = [L.to_real('cases', index_fields(L, s, {}, L.values['staging_publication'], schema)) for s in eligible if s['index'] is None]
            if staged:
                commands.append(io.batch_create(L.base('cases'), L.table('cases'), staged, dry_run=True))
            commands.append(io.batch_update(L.base('cases'), L.table('cases'), [dict(record_id=s['index']['record_id'] if s['index'] else '<staged-record>',
                                                                                    fields=L.to_real('cases', index_fields(L, s, {}, L.published_value, schema))) for s in eligible], dry_run=True))
            if repairs:
                commands.append(io.batch_update(L.base('review'), L.table('review'), [dict(record_id=s['record_id'], fields=L.to_real('review', {'入库状态': [L.values['ingest_done']]})) for s in repairs], dry_run=True))
            report.update(status='ok', commands=commands, steps=['register publish batch', 'stage unpublished index rows for new cases', 'insert and verify body sections',
                                                                 'ensure catalog rows', 'publish index rows', 'sync document revisions', 'mark review rows ingested', 'readback'])
            return report
        batch_id = 'publish-' + uuid.uuid4().hex
        wrote = True
        m.require_batch(io.batch_create(L.base('batches'), L.table('batches'), [L.to_real('batches', {
            '批次ID': batch_id, '批次状态': [L.values['batch_running']], '总用例数': len(eligible), '成功数': 0, '失败数': 0,
            '待确认数': 0, '备注': (selection or {}).get('note', 'publish'), '检查点': 'publish:registered'})]))
        found = find_rows(L, 'batches', '批次ID', batch_id)
        if len(found) != 1:
            raise io.LarkError('batch_readback', 'publish batch unconfirmed; no further writes')
        batch_record = found[0]['record_id']; report['batch_id'] = batch_id
        repaired = repair_ledger(L, repairs, report)
        # Stage new index rows as unpublished so body links can target a real record.
        staging = [s for s in eligible if s['index'] is None]
        if staging:
            create = [L.to_real('cases', index_fields(L, s, {}, L.values['staging_publication'], schema)) for s in staging]
            m.require_batch(io.batch_create(L.base('cases'), L.table('cases'), create))
            staged, _ = read_tables(L, ['cases'])
            for s in staging:
                hits = [r for r in staged['cases'] if plain(r['fields'].get('系统用例ID')) == s['system_id']]
                if len(hits) != 1:
                    raise io.LarkError('staging_readback', 'staged index row not found exactly once', detail=dict(system_id=s['system_id'], found=len(hits)))
                s['index'] = hits[0]; s['staged'] = True
        verified, documents = write_bodies(L, assignments, report) if assignments else ([], {})
        # Re-read the human review right before the formal index changes.
        fresh, _ = read_tables(L, ['review'])
        by_record = {r['record_id']: r for r in fresh['review']}
        final = []
        for s in verified:
            record = by_record.get(s['record_id'])
            again = review_state(L, record) if record else dict(problems=['review row disappeared'])
            if again['problems'] or any(again.get(k) != s.get(k) for k in ('content_hash', 'meta_hash', 'structured_hash', 'submit_key')):
                report['rejected'].append(dict(system_id=s['system_id'], display_id=s['case']['用例编号'], reasons=['review changed during publish'] + again['problems'], body=s['section_url']))
            else:
                final.append(s)
        ensure_catalog(L, final, rows['catalog'])
        updates = [dict(record_id=s['index']['record_id'], fields=L.to_real('cases', index_fields(L, s, {
            '正文链接': f'[{s["case"]["用例编号"]} {s["case"]["用例标题"]}]({s["section_url"]})', '文档token': s['doc'],
            '章节block ID': s['anchor'], '文档版本': str(documents[s['doc']]['revision'])}, L.published_value, schema))) for s in final]
        if updates:
            m.require_batch(io.batch_update(L.base('cases'), L.table('cases'), updates))
        sync = sync_revisions(L, documents)
        reviews = [dict(record_id=s['record_id'], fields=L.to_real('review', {
            '入库状态': [L.values['ingest_done']], '正式索引链接': L.record_url('cases', s['index']['record_id']),
            '正文链接': s['section_url'], '文档token': s['doc'], '章节block ID': s['anchor']})) for s in final]
        if reviews:
            m.require_batch(io.batch_update(L.base('review'), L.table('review'), reviews))
        report['published'] = [dict(system_id=s['system_id'], display_id=s['case']['用例编号'], version=s['version'], section=s['section_url'],
                                    index_locator=L.locator('cases', s['index']['record_id'])) for s in final]
        report['readback'] = verify_publish(L, updates + sync, reviews, documents)
        failed = report['readback']['errors'] or len(verified) < len(eligible) or len(final) < len(verified) or len(repaired) < len(repairs)
        report['status'] = 'partial' if failed else 'ok'
    except io.LarkError as exc:
        report.update(status='partial' if wrote else 'failed', error=exc.as_dict())
        if isinstance(exc.detail, dict) and exc.detail.get('unknown_outcome'):
            report['unknown_outcome'] = 'write outcome unknown; nothing was retried; read back before rerunning'
            try:
                probe, _ = read_tables(L, ['cases'], strict=False)
                wanted_ids = {s['system_id'] for s in eligible}
                report['unknown_outcome_readback'] = dict(cases=[dict(system_id=plain(r['fields'].get('系统用例ID')), status=plain(r['fields'].get('发布状态')),
                                                                      version=plain(r['fields'].get('内容版本')), locator=L.locator('cases', r['record_id']))
                                                                 for r in probe['cases'] if plain(r['fields'].get('系统用例ID')) in wanted_ids])
            except io.LarkError as probe_error:
                report['unknown_outcome_readback'] = probe_error.as_dict()
    except (ValueError, OSError, TypeError) as exc:
        report.update(status='partial' if wrote else 'failed', input_error=str(exc))
    finally:
        if batch_record:
            finish_batch(L, batch_record, report, len(report['published']))
    return report


def repair_ledger(L, repairs, report):
    """Index already published this exact reviewed version; verify its body, then mark the review row ingested."""
    report['ledger_repairs'] = []
    done = []
    for s in repairs:
        f = s['index']['fields']
        token, anchor = doc_token(f.get('文档token')) or doc_token(first_url(f.get('正文链接'))), plain(f.get('章节block ID'))
        record_url = L.record_url('cases', s['index']['record_id'])
        entry = dict(system_id=s['system_id'], display_id=s['case']['用例编号'], version=s['version'], review_locator=L.locator('review', s['record_id']))
        try:
            owned = owned_sections(fetch_blocks(token)['content'], heading_text(s['case'], s['version']), record_url) if token else []
        except io.LarkError as exc:
            entry.update(status='failed', error=exc.as_dict()); report['ledger_repairs'].append(entry); continue
        if len(owned) != 1 or owned[0]['id'] != anchor or not section_matches(owned[0], s['case'], s['version']):
            entry.update(status='failed', error='published body section not verifiable by heading, record backlink and reviewed content'); report['ledger_repairs'].append(entry); continue
        fresh = [r for r in find_rows(L, 'review', '系统用例ID', s['system_id']) if r['record_id'] == s['record_id']]
        again = review_state(L, dict(record_id=s['record_id'], fields=L.to_logical('review', fresh[0]['fields']))) if len(fresh) == 1 else dict(problems=['review row not found'])
        if again['problems'] or any(again.get(k) != s.get(k) for k in ('content_hash', 'meta_hash', 'structured_hash', 'submit_key')):
            entry.update(status='failed', error='review changed before ledger repair', reasons=again['problems']); report['ledger_repairs'].append(entry); continue
        fields = L.to_real('review', {'入库状态': [L.values['ingest_done']], '正式索引链接': record_url, '正文链接': first_url(f.get('正文链接')),
                                      '文档token': token, '章节block ID': anchor})
        m.require_batch(io.batch_update(L.base('review'), L.table('review'), [dict(record_id=s['record_id'], fields=fields)]))
        back = [r for r in find_rows(L, 'review', '系统用例ID', s['system_id']) if r['record_id'] == s['record_id']]
        ok = len(back) == 1 and all(plain(back[0]['fields'].get(k)) == plain(v) for k, v in fields.items())
        entry.update(status='repaired' if ok else 'readback_mismatch'); report['ledger_repairs'].append(entry)
        if ok:
            done.append(s)
    return done


def assign_volumes(L, eligible, index_rows):
    """Revisions stay in their volume; new cases go to their group's last volume, overflow to new ones."""
    counts, groups = {}, {}
    for record in index_rows:
        token = doc_token(record['fields'].get('文档token')) or doc_token(first_url(record['fields'].get('正文链接')))
        if not token:
            continue
        counts[token] = counts.get(token, 0) + 1
        key = tuple(plain(record['fields'].get(k)) for k in ('项目', '模块', '功能点'))
        if token not in groups.setdefault(key, []):
            groups[key].append(token)
    assignments = []
    for s in sorted(eligible, key=lambda s: (s['case']['项目'], s['case']['模块'], s['case']['功能'], s['case']['用例编号'])):
        current = s['index']
        token = current and (doc_token(current['fields'].get('文档token')) or doc_token(first_url(current['fields'].get('正文链接'))))
        key = (s['case']['项目'], s['case']['模块'], s['case']['功能'])
        if token:
            assignments.append((s, dict(kind='revision', doc=token, label=token, group=key, existing_volumes=len(groups.get(key, [])))))
            continue
        candidates = groups.get(key, [])
        target = candidates[-1] if candidates and counts.get(candidates[-1], 0) < L.max_cases else None
        if target:
            counts[target] += 1
        assignments.append((s, dict(kind='new', doc=target, label=target or 'new volume', group=key, existing_volumes=len(candidates))))
    return assignments


def write_bodies(L, assignments, report):
    """Write sections; a failed document stops all further writes and none of its cases publish."""
    documents, verified, created = {}, [], {}
    report['documents'] = []
    links_base = [('返回检索浏览', L.browse_url()), ('返回正文目录', L.catalog_url())]
    current = None

    numbers = {}

    def fits(state, size, s):
        if owned_sections(state['blocks']['content'], heading_text(s['case'], s['version']), L.record_url('cases', s['index']['record_id'])):
            return True  # recovery of an earlier unknown write; insert_section validates it
        count = case_sections(state['blocks'])
        return count == 0 or (count < L.max_cases and state['blocks']['length'] + size <= L.max_chars)

    def volume_for(s, assignment, size):
        nonlocal current
        key = assignment['group']
        volume = created.get(key)
        if volume and fits(documents[volume['doc']], size, s):
            return volume['doc'], volume['url']
        for _ in range(100):
            numbers[key] = numbers.get(key, assignment['existing_volumes']) + 1
            title = ' '.join(x for x in [L.name, s['case']['所属分类'] or s['case']['功能'], f'第{numbers[key]}卷'] if x)
            header = f'<p>{" · ".join(link_xml(u, t) for t, u in links_base if u)}</p>' if any(u for _, u in links_base) else '<p>用例正文分卷</p>'
            made = create_volume(L, title, header)
            current = made['token']
            state = documents.get(made['token']) or load_document(L, made['token'], documents)
            report['documents'].append(dict(action='create_volume', group=list(key), title=title, doc=made['token'], url=made['url'], recovered=made['recovered']))
            if not made['recovered']:
                append_catalog(L, dict(title=title, url=made['url']), report)
            elif not fits(state, size, s):
                continue  # an existing same-title volume is full; never overwrite it, use the next number
            else:
                append_catalog(L, dict(title=title, url=made['url']), report)
            created[key] = dict(doc=made['token'], url=made['url'])
            return made['token'], made['url']
        raise io.LarkError('volume_numbering', 'no free volume number found for group', detail=dict(group=list(key)))

    try:
        for s, assignment in assignments:
            record_url = L.record_url('cases', s['index']['record_id'])
            links = [(f'返回用例索引（{s["case"]["用例编号"]}）', record_url), *links_base]
            size = len(section_xml(s['case'], s['version'], links))
            doc, url = assignment['doc'], None
            if doc:
                current = doc
                state = documents.get(doc) or load_document(L, doc, documents)
                if not fits(state, size, s):
                    doc = None
            if not doc:
                current = None
                doc, url = volume_for(s, assignment, size)
                current = doc
            state = documents.get(doc) or load_document(L, doc, documents)
            result = insert_section(L, doc, s['case'], s['version'], links, state['blocks'], record_url)
            state['blocks'] = result['blocks']; state['sections'] += 1
            s.update(doc=doc, anchor=result['anchor'], section_url=(url or L.doc_url(doc)).split('#')[0] + '#' + result['anchor'])
            verified.append(s)
    except io.LarkError as exc:
        report['body_error'] = dict(exc.as_dict(), doc=current)
        report['unpublished_after_body_error'] = [dict(system_id=s['system_id'], display_id=s['case']['用例编号']) for s, _ in assignments if s not in verified]
        if current in documents:
            documents[current]['failed'] = True
        if isinstance(exc.detail, dict) and exc.detail.get('unknown_outcome'):
            report['unknown_outcome'] = 'document write outcome unknown; nothing was retried'
    for doc, state in documents.items():
        if state.get('failed'):
            continue
        try:
            final = fetch_blocks(doc)
            lost = state['before_ids'] - set(final['ids'])
            if lost:
                raise io.LarkError('anchors_lost', 'existing anchors disappeared during publish', detail=dict(doc=doc, lost=sorted(lost)[:20]))
            if state['nav'] != [h['text'] for h in final['headings'] if h['text'] in L.nav_headings]:
                raise io.LarkError('navigation_changed', 'navigation headings changed during publish', detail=dict(doc=doc))
            state.update(revision=final['revision'], blocks=final)
            report['documents'].append(dict(action='sections_verified', doc=doc, sections=state['sections'], revision=final['revision'], anchors_preserved=len(state['before_ids'])))
        except io.LarkError as exc:
            state['failed'] = True
            report.setdefault('document_errors', []).append(dict(exc.as_dict(), doc=doc))
    failed = {doc for doc, state in documents.items() if state.get('failed')}
    report['failed_documents'] = sorted(failed)
    verified = [s for s in verified if s['doc'] not in failed]
    return verified, {doc: state for doc, state in documents.items() if doc not in failed}


def load_document(L, doc, documents):
    blocks = fetch_blocks(doc)
    documents[doc] = dict(before_ids=set(blocks['ids']), nav=[h['text'] for h in blocks['headings'] if h['text'] in L.nav_headings], blocks=blocks, sections=0)
    return documents[doc]


def append_catalog(L, volume, report):
    catalog = L.options.get('catalog_doc')
    if not catalog:
        return
    doc = doc_token(catalog) or catalog
    before = fetch_blocks(doc)
    label = volume['title']
    if any(label in h['text'] for h in before['headings']) or esc(label) in before['content']:
        return
    content = f'<p>{link_xml(volume["url"], label)}</p>'
    args = ['docs', '+update', '--doc', doc, '--command', 'append', '--content', '-']
    if before.get('revision') is not None:
        args += ['--revision-id', str(before['revision'])]
    doc_write(args, content, lambda: esc(label) in fetch_blocks(doc)['content'])
    after = fetch_blocks(doc)
    if set(before['ids']) - set(after['ids']) or esc(label) not in after['content']:
        raise io.LarkError('catalog_readback', 'catalog document link not confirmed or anchors lost', detail=dict(doc=doc))
    report['documents'].append(dict(action='catalog_link', doc=doc, volume=label))


def ensure_catalog(L, states, catalog_rows):
    existing = {plain(r['fields'].get('分类ID')) for r in catalog_rows}
    rows, seen = [], set()
    for s in states:
        category = s['structured'].get('category') or {}
        cid = category.get('分类路径') or s['case']['所属分类']
        if not cid or cid in existing or cid in seen:
            continue
        seen.add(cid)
        rows.append(L.to_real('catalog', {'分类ID': cid, '项目': s['case']['项目'], '模块': s['case']['模块'], '功能点': s['case']['功能'],
                                          '分类说明': category.get('分类说明', ''), '别名与同义词': category.get('关键词', ''),
                                          '适用版本': s['case']['适用版本'], '状态': [L.values['category_enabled']]}))
    if rows:
        m.require_batch(io.batch_create(L.base('catalog'), L.table('catalog'), rows))


def sync_revisions(L, documents):
    """Every index row pointing at a changed volume carries its new revision."""
    if not documents:
        return []
    current, _ = read_tables(L, ['cases'])
    updates = []
    for record in current['cases']:
        token = doc_token(record['fields'].get('文档token'))
        state = documents.get(token)
        if state and plain(record['fields'].get('文档版本')) != str(state['revision']):
            updates.append(dict(record_id=record['record_id'], fields=L.to_real('cases', {'文档版本': str(state['revision'])})))
    if updates:
        m.require_batch(io.batch_update(L.base('cases'), L.table('cases'), updates))
    return updates


def verify_publish(L, case_updates, review_updates, documents):
    errors = []
    after, _ = read_tables(L, ['cases', 'review'])
    for key, updates in (('cases', case_updates), ('review', review_updates)):
        actual = {r['record_id']: r['raw'] for r in after[key]}
        for update in updates:
            row = actual.get(update['record_id'], {})
            for name, value in update['fields'].items():
                if plain(row.get(name)) != plain(value):
                    errors.append(dict(locator=L.locator(key, update['record_id']), field=name, error='readback mismatch'))
    for doc, state in documents.items():
        content = state['blocks']['content']
        for update in case_updates:
            fields = L.to_logical('cases', update['fields'])
            if fields.get('文档token') == doc and fields.get('章节block ID') and f'id="{fields["章节block ID"]}"' not in content:
                errors.append(dict(doc=doc, anchor=fields['章节block ID'], error='anchor missing'))
    return dict(complete=True, errors=errors)


# ---------------------------------------------------------------- browse view

def manage_browse_view(lib, *, dry_run=False):
    L = validate_config(lib)
    browse = L.options.get('browse') or {}
    if not browse:
        raise ValueError('maintain.browse (view and fields) is not configured; the browse view is not managed by this script')
    report = dict(status='failed', profile=PROFILE, dry_run=dry_run, view=browse['view'], locator=L.locator('cases'))
    schema, mismatch = read_schema(L, ['cases'])
    names = [f.get('name') for f in schema['cases'] if isinstance(f, dict)]
    wanted = [L.real('cases', n) for n in browse['fields']]
    technical = [n for n in browse['fields'] if n in TECHNICAL]
    unknown = [n for n in wanted if n not in names]
    if technical or unknown or len(set(wanted)) != len(wanted):
        raise ValueError(f'browse fields invalid: technical={technical} unknown={unknown}; daily browsing hides technical IDs')
    views = io.data(io.run_lark(['base', '+view-list', '--base-token', L.base('cases'), '--table-id', L.table('cases')]))
    view_list = views.get('views', views.get('items', []))
    target = [v for v in view_list if browse['view'] in (v.get('id'), v.get('view_id'))]
    full = browse.get('full_view')
    if len(target) != 1:
        raise ValueError('configured browse view not found exactly once')
    if full and full == browse['view']:
        raise ValueError('browse view must differ from the full-field view')
    current = io.data(io.run_lark(['base', '+view-get-visible-fields', '--base-token', L.base('cases'), '--table-id', L.table('cases'), '--view-id', browse['view']]))
    report.update(before=current.get('visible_fields'), wanted=wanted, field_count=len(names))
    if full:
        full_fields = io.data(io.run_lark(['base', '+view-get-visible-fields', '--base-token', L.base('cases'), '--table-id', L.table('cases'), '--view-id', full])).get('visible_fields', [])
        report['full_view'] = dict(view=full, visible=len(full_fields), table_fields=len(names), complete=set(names) <= set(full_fields))
    if current.get('visible_fields') == wanted:
        report['status'] = 'ok'; report['action'] = 'unchanged'; return report
    args = ['base', '+view-set-visible-fields', '--base-token', L.base('cases'), '--table-id', L.table('cases'), '--view-id', browse['view'],
            '--json', json.dumps({'visible_fields': wanted}, ensure_ascii=False)]
    if dry_run:
        report.update(status='ok', action='set_visible_fields', command=io.run_lark(args, dry_run=True)); return report
    try:
        io.run_lark(args)
    except io.LarkError as exc:
        report.update(status='partial' if io._unknown_write(exc) else 'failed', error=exc.as_dict()); return report
    after = io.data(io.run_lark(['base', '+view-get-visible-fields', '--base-token', L.base('cases'), '--table-id', L.table('cases'), '--view-id', browse['view']]))
    report.update(after=after.get('visible_fields'), action='set_visible_fields', status='ok' if after.get('visible_fields') == wanted else 'partial')
    return report


# ---------------------------------------------------------------- inspect / check / export

def inspect(lib):
    L = validate_config(lib)
    schema, mismatch = read_schema(L, L.keys())
    rows, errors = read_tables(L, L.keys(), strict=False)
    return dict(status='partial' if errors else 'ok', profile=PROFILE, complete=not errors, read_errors=errors, schema_mismatch=mismatch,
                locators={k: L.locator(k) for k in L.keys()}, counts={k: len(v) for k, v in rows.items()},
                categories=[r['fields'] for r in rows.get('catalog', [])], field_maps=L.maps,
                fields={k: [f.get('name') for f in v if isinstance(f, dict)] for k, v in schema.items()})


def check(lib):
    L = validate_config(lib)
    rows, errors = read_tables(L, L.keys(), strict=False)
    issues, info = [], dict(legacy_review_rows=0, duplicate_display_ids=[])
    index = rows.get('cases', [])
    by_sid = {}
    for record in index:
        by_sid.setdefault(plain(record['fields'].get('系统用例ID')), []).append(record)
    for sid, records in by_sid.items():
        if not sid:
            issues += [dict(locator=L.locator('cases', r['record_id']), issue='empty system id') for r in records]
        elif len(records) > 1:
            issues.append(dict(system_id=sid, issue='duplicate system id in index', locators=[L.locator('cases', r['record_id']) for r in records]))
    displays = {}
    for record in index:
        displays.setdefault(plain(record['fields'].get('原用例编号')), []).append(plain(record['fields'].get('系统用例ID')))
    info['duplicate_display_ids'] = sorted(k for k, v in displays.items() if len(v) > 1)
    histories = latest_reviews(rows.get('review', []))
    for record in rows.get('review', []):
        state = review_state(L, record)
        if any('legacy' in p for p in state['problems']):
            info['legacy_review_rows'] += 1; continue
        changed = [p for p in state['problems'] if 'changed after submission' in p or 'does not match' in p]
        if changed:
            issues.append(dict(locator=L.locator('review', record['record_id']), system_id=state['system_id'], issue='; '.join(changed)))
    docs = {}
    for record in index:
        f = record['fields']
        published = plain(f.get('发布状态')) == L.published_value
        if not published:
            continue
        token, anchor = doc_token(f.get('文档token')) or doc_token(first_url(f.get('正文链接'))), plain(f.get('章节block ID'))
        url = first_url(f.get('正文链接'))
        if not token or not anchor or not url:
            issues.append(dict(locator=L.locator('cases', record['record_id']), issue='published row lacks body link, document token or anchor')); continue
        if not url.endswith('#' + anchor):
            issues.append(dict(locator=L.locator('cases', record['record_id']), issue='body link anchor differs from 章节block ID'))
        docs.setdefault(token, []).append(record)
        history = histories.get(plain(f.get('系统用例ID')), [])
        match = [r for r in history if plain(r['fields'].get('内容版本')) == plain(f.get('内容版本'))]
        if not match:
            issues.append(dict(locator=L.locator('cases', record['record_id']), issue='published version has no review row'))
        elif plain(match[-1]['fields'].get('审核状态')) != L.values['review_approved']:
            issues.append(dict(locator=L.locator('cases', record['record_id']), issue='published version is not approved in review'))
        elif plain(match[-1]['fields'].get('内容哈希')) != plain(f.get('内容哈希')):
            issues.append(dict(locator=L.locator('cases', record['record_id']), issue='index content hash differs from reviewed version'))
    doc_errors = {}
    for token, records in docs.items():
        try:
            blocks = fetch_blocks(token)
        except io.LarkError as exc:
            doc_errors[token] = exc.as_dict(); continue
        ids = set(blocks['ids'])
        by_anchor = {h['id']: h for h in sections(blocks['content']) if h['id']}
        anchors = {}
        for record in records:
            anchors.setdefault(plain(record['fields'].get('章节block ID')), []).append(record['record_id'])
        for anchor, owners in anchors.items():
            if len(owners) > 1:
                issues.append(dict(doc=token, anchor=anchor, issue='several index records share one body section', locators=[L.locator('cases', r) for r in owners]))
        for record in records:
            f = record['fields']
            locator = L.locator('cases', record['record_id'])
            anchor = plain(f.get('章节block ID'))
            if anchor not in ids:
                issues.append(dict(locator=locator, doc=token, issue='section anchor missing in body')); continue
            backlink = L.record_url('cases', record['record_id']) or 'record=' + record['record_id']
            section = by_anchor.get(anchor)
            version = re.search(r'（内容版本 (\d+)）$', section['text']) if section else None
            if version:
                # Sections written by this Skill carry their own record backlink and version.
                if not backlinks(section['body'], backlink):
                    issues.append(dict(locator=locator, doc=token, issue='body section does not link back to its index record'))
                if version.group(1) != plain(f.get('内容版本')):
                    issues.append(dict(locator=locator, doc=token, issue='body section version differs from index', section_version=version.group(1)))
                reviewed = [r for r in histories.get(plain(f.get('系统用例ID')), []) if plain(r['fields'].get('内容版本')) == version.group(1)]
                state = review_state(L, reviewed[-1]) if reviewed else {}
                if state.get('case') and not section_matches(section, state['case'], version.group(1)):
                    issues.append(dict(locator=locator, doc=token, issue='body section content differs from reviewed version'))
            elif not backlinks(html.unescape(blocks['content']), backlink):
                issues.append(dict(locator=locator, doc=token, issue='body lacks link back to index record'))
            if plain(f.get('文档版本')) != str(blocks['revision']):
                issues.append(dict(locator=L.locator('cases', record['record_id']), doc=token, issue='document revision out of sync', recorded=plain(f.get('文档版本')), actual=blocks['revision']))
    if 'routes' in rows:
        issues += [dict(issue=p) for p in check_routes(L, rows, 0)]
    complete = not errors and not doc_errors
    return dict(status='ok' if complete else 'partial', profile=PROFILE, complete=complete, read_errors=errors, document_errors=doc_errors,
                issues=issues, info=info, counts={k: len(v) for k, v in rows.items()}, documents=len(docs))


def export(lib, out_dir):
    L = validate_config(lib)
    directory = Path(out_dir); directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(status='ok', profile=PROFILE, library=L.name, files=[], errors=[], complete=True, field_maps=L.maps)
    docs = []
    for key in L.keys():
        result = io.list_records(L.base(key), L.table(key))
        path = directory / (key + '.ndjson')
        path.write_text(''.join(m.json_text(dict(r, logical=L.to_logical(key, r['fields']), locator=L.locator(key, r['record_id']))) + '\n' for r in result['records']), encoding='utf-8')
        manifest['files'].append(dict(path=path.name, locator=L.locator(key), count=len(result['records']), sha256=sha256_file(path), complete=result['complete']))
        if not result['complete']:
            manifest['errors'].append(dict(table=key, error=result['error']))
        if key == 'cases':
            for record in result['records']:
                f = L.to_logical(key, record['fields'])
                token = doc_token(f.get('文档token')) or doc_token(first_url(f.get('正文链接')))
                if token and token not in docs:
                    docs.append(token)
    for index, token in enumerate(docs, 1):
        try:
            blocks = fetch_blocks(token)
            if not blocks['content']:
                raise io.LarkError('empty_body', 'document content empty')
            body = directory / f'body-{index:04d}.xml'; body.write_text(blocks['content'], encoding='utf-8')
            manifest['files'].append(dict(path=body.name, doc=token, revision=blocks['revision'], sha256=sha256_file(body), complete=True))
        except io.LarkError as exc:
            manifest['errors'].append(dict(doc=token, error=exc.as_dict()))
    if manifest['errors']:
        manifest.update(status='partial', complete=False)
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest
