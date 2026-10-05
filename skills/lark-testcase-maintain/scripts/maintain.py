"""Mechanical standard-library maintenance; Agent supplies all interpretation."""
import argparse
import copy
import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
import lark_io as io

CATALOG = ['分类路径', '分类说明', '关键词', '用例数', '正文文档', '更新时间']
CASES = ['用例编号', '用例标题', '所属分类', '项目', '模块', '功能', '测试类型', '设计方法', '需求编号', '优先级', '前置条件', '测试步骤', '预期结果', '适用版本', '来源版本', '关键词', '摘要', '扩展字段', '来源文件', '来源位置', '正文文档', '审核状态', '状态', '入库批次', '内容哈希', '入库时间']
BATCHES = ['批次号', '来源文件', '原件链接', '原件SHA256', '新增数', '更新数', '未变数', '冲突数', '导入时间', '执行结果', '说明']
TABLE_NAMES = dict(catalog='用例库目录', cases='测试用例', batches='导入批次')
SELECTS = {'审核状态': ['待审核', '已审核', '需修改'], '状态': ['有效', '已废弃']}
NUMBERS = {'用例数', '新增数', '更新数', '未变数', '冲突数'}
STANDARD_TABLES = {k: [{'name': n, 'type': 'select' if n in SELECTS else 'number' if n in NUMBERS else 'text', **({'multiple': False, 'options': [{'name': x} for x in SELECTS[n]]} if n in SELECTS else {})} for n in names] for k, names in dict(catalog=CATALOG, cases=CASES, batches=BATCHES).items()}
HASH_EXCLUDED = {'用例编号', '来源文件', '来源位置', '正文文档', '审核状态', '入库批次', '内容哈希', '入库时间'}
BUSINESS = [n for n in CASES if n not in HASH_EXCLUDED]
ARRAY_TEXT = {'测试步骤', '预期结果', '需求编号', '设计方法'}


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalized(case):
    result = dict(case)
    for key in ARRAY_TEXT:
        value = result.get(key)
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ValueError(key + ': array items must be strings')
            result[key] = '\n'.join(f'{i}. {item}' for i, item in enumerate(value, 1)) if key in ('测试步骤', '预期结果') else '、'.join(value)
    for key, default in (('状态', '有效'), ('审核状态', '待审核')):
        value = result.get(key)
        if isinstance(value, list):
            value = value[0] if value else ''
        result[key] = value or default
    for key in CASES:
        if key not in SELECTS and key != '扩展字段':
            result[key] = result.get(key) if result.get(key) is not None else ''
    extension = result.get('扩展字段') or {}
    if isinstance(extension, str):
        extension = json.loads(extension)
    result['扩展字段'] = extension
    return result


def content_hash(case):
    c = normalized(case)
    return hashlib.sha256(json_text({k: c[k] for k in BUSINESS}).encode('utf-8')).hexdigest()


def to_remote(case):
    result = {k: v for k, v in case.items() if k in CASES}
    for k in SELECTS:
        if k in result and not isinstance(result[k], list):
            result[k] = [result[k]]
    if '扩展字段' in result:
        result['扩展字段'] = json_text(result['扩展字段'])
    return result


def items(payload, key):
    if isinstance(payload, list):
        return payload
    value = payload.get(key, payload.get('items', []))
    if not isinstance(value, list):
        raise io.LarkError('response_shape', f'missing {key} list')
    return value


def get_id(payload, *names):
    if isinstance(payload, dict):
        for name in names:
            if isinstance(payload.get(name), str) and payload[name]:
                return payload[name]
        for value in payload.values():
            try:
                return get_id(value, *names)
            except io.LarkError:
                pass
    raise io.LarkError('response_shape', f'missing identifier: {names}')


def get_url(payload, kind):
    try:
        return get_id(payload, 'url', 'document_url', 'file_url')
    except io.LarkError:
        raise io.LarkError('response_shape', 'created resource URL missing; inspect returned resource identifiers', detail=payload)


def doc_ref(value):
    match = re.search(r'https?://[^\s)]+', value or '')
    return match.group() if match else value


def library_config(config, library):
    if config.get('config_version') != '2.0':
        raise ValueError('config_version must be 2.0')
    matches = [lib for lib in config.get('libraries', []) if lib.get('name') == library]
    if len(matches) != 1:
        raise ValueError('library name must resolve uniquely')
    lib = matches[0]
    if lib.get('kind') != 'standard':
        raise ValueError('Maintain requires kind=standard')
    profile = lib.get('schema_profile', 'standard')
    if profile == 'indexed':
        import indexed
        indexed.validate_config(lib)
        return lib
    if profile != 'standard':
        raise ValueError('schema_profile must be standard or indexed')
    if lib.get('table_base_tokens') or lib.get('field_maps'):
        raise ValueError('table_base_tokens/field_maps are supported only with schema_profile=indexed')
    if not lib.get('base_token') or any(not lib.get('tables', {}).get(k) for k in TABLE_NAMES) or any(not lib.get('folders', {}).get(k) for k in ('root', 'originals', 'bodies')):
        raise ValueError('standard library requires base, three tables and three folders')
    if any('<' in str(v) for v in [lib['base_token'], *lib['tables'].values(), *lib['folders'].values()]):
        raise ValueError('example placeholder identifiers cannot be executed')
    return lib


def read_all(lib):
    result = {k: io.list_records(lib['base_token'], table) for k, table in lib['tables'].items() if k in TABLE_NAMES}
    incomplete = {k: v for k, v in result.items() if not v['complete']}
    if incomplete:
        raise io.LarkError('incomplete_read', 'table read incomplete; no writes allowed', detail=incomplete)
    return {k: v['records'] for k, v in result.items()}


def field_names(fields):
    return {f if isinstance(f, str) else f.get('name', f.get('field_name')) for f in fields}


def schema(lib):
    result = {k: items(io.list_fields(lib['base_token'], t), 'fields') for k, t in lib['tables'].items() if k in TABLE_NAMES}
    return result



def schema_mismatch(actual):
    errors = {}
    for key, expected in STANDARD_TABLES.items():
        found = {f.get('name'): f for f in actual[key] if isinstance(f, dict)}
        mismatches = []
        for field in expected:
            observed = found.get(field['name'])
            if not observed or observed.get('type') != field['type']:
                mismatches.append(field['name'] + ': missing or wrong type')
            elif field['type'] == 'select' and observed.get('multiple', False):
                mismatches.append(field['name'] + ': expected single select')
        if mismatches:
            errors[key] = mismatches
    return errors

def validate_plan(plan, fields_by_table):
    errors = []
    def err(path, message):
        errors.append(dict(path=path, message=message))
    if not isinstance(plan, dict):
        return [dict(path='$', message='plan must be an object')]
    if plan.get('plan_version') != '2.0':
        err('plan_version', 'must be 2.0')
    if not isinstance(plan.get('library'), str) or not plan.get('library'):
        err('library', 'nonempty string required')
    batch = plan.get('batch', {})
    if not isinstance(batch, dict) or any(not isinstance(batch.get(k), str) or not batch[k] for k in ('name', 'source_file')):
        err('batch', 'name and source_file required')
    targets = plan.get('targets')
    if not isinstance(targets, dict) or any(type(targets.get(k)) is not bool for k in ('base', 'doc')) or not any(targets.values()):
        err('targets', 'base/doc booleans required; at least one true')
    if plan.get('on_conflict', 'update') not in ('update', 'skip', 'fail'):
        err('on_conflict', 'must be update/skip/fail')
    categories = plan.get('categories')
    cases = plan.get('cases')
    if not isinstance(categories, list) or not isinstance(cases, list):
        err('$', 'categories and cases must be arrays'); return errors
    allowed = field_names(fields_by_table.get('cases', CASES))
    category_names = set(fields_by_table.get('existing_categories', []))
    seen_categories, seen = set(), set()
    for i, category in enumerate(categories):
        if not isinstance(category, dict) or not isinstance(category.get('分类路径'), str) or not category.get('分类路径'):
            err(f'categories[{i}]', '分类路径 required'); continue
        if category['分类路径'] in seen_categories:
            err(f'categories[{i}]', 'duplicate category')
        seen_categories.add(category['分类路径']); category_names.add(category['分类路径'])
        if set(category) - set(CATALOG):
            err(f'categories[{i}]', 'unknown category fields')
    for i, case in enumerate(cases):
        path = f'cases[{i}]'
        if not isinstance(case, dict):
            err(path, 'case must be object'); continue
        for key in ('用例编号', '用例标题', '所属分类', '来源位置'):
            if not isinstance(case.get(key), str) or not case[key].strip():
                err(path + '.' + key, 'nonempty string required')
        if not case.get('测试步骤') and not case.get('预期结果'):
            err(path, '测试步骤/预期结果 at least one required')
        if case.get('用例编号') in seen:
            err(path, 'duplicate 用例编号')
        seen.add(case.get('用例编号'))
        if case.get('所属分类') not in category_names:
            err(path, 'category not in plan or library')
        if set(case) - allowed:
            err(path, 'unknown fields; preserve them inside 扩展字段: ' + ','.join(sorted(set(case) - allowed)))
        for key, value in case.items():
            if key in SELECTS and value not in SELECTS[key]:
                err(path + '.' + key, 'invalid selection')
            elif key in ARRAY_TEXT and isinstance(value, list):
                if not all(isinstance(item, str) for item in value):
                    err(path + '.' + key, 'array items must be strings')
            elif key != '扩展字段' and key not in SELECTS and not isinstance(value, (str, type(None))):
                err(path + '.' + key, 'text field must be string or null')
        try:
            extension = case.get('扩展字段', {})
            if not isinstance(extension, dict):
                err(path + '.扩展字段', 'must be JSON object')
            json_text(extension)
        except (ValueError, TypeError):
            err(path + '.扩展字段', 'must be serializable JSON')
    return errors


def plan_operations(plan, existing_records):
    by_id = {}
    duplicate = set()
    for record in existing_records:
        key = record['fields'].get('用例编号')
        if key in by_id:
            duplicate.add(key)
        by_id[key] = record
    operations = []
    for raw in plan['cases']:
        case = normalized(raw)
        key, hashed = case['用例编号'], content_hash(case)
        old = by_id.get(key)
        action = 'create' if old is None else 'unchanged' if content_hash(old['fields']) == hashed else {'update': 'update', 'skip': 'skip', 'fail': 'conflict'}[plan.get('on_conflict', 'update')]
        if key in duplicate:
            action = 'conflict'
        operations.append(dict(case_id=key, action=action, content_hash=hashed, case=case, record_id=old['record_id'] if old else None, source_changed=bool(old and case.get('来源文件') and case['来源文件'].split(' http', 1)[0] != str(old['fields'].get('来源文件', '')).split(' http', 1)[0])))
    return dict(operations=operations, counts={a: sum(o['action'] == a for o in operations) for a in ('create', 'update', 'unchanged', 'skip', 'conflict')})


def md(value):
    text = json_text(value) if isinstance(value, (dict, list)) else str('' if value is None else value)
    # Lark Markdown treats XML tags specially; backslash escapes preserve raw text.
    text = re.sub(r'([\\`*_\[\]$~<|])', r'\\\1', text)
    text = re.sub(r'(?m)^(\s*)([#+>\-])', r'\1\\\2', text)
    return text.replace('\n', '<br>')


def render_category_markdown(category, cases, library_name):
    lines = [f'# {md(library_name)} / {md(category["分类路径"])}', '', md(category.get('分类说明', '')), '', f'用例数：{len(cases)}', '']
    for case in cases:
        lines += [f'### {md(case["用例编号"])} {md(case["用例标题"])}', '', '| 字段 | 原值 |', '| --- | --- |']
        for key in CASES:
            if key in ('测试步骤', '预期结果'):
                continue
            value = case.get(key, '')
            if key in ('适用版本', '来源版本') and not value:
                value = '未提供'
            if value not in ('', None, [], {}):
                lines.append(f'| {key} | {md(value)} |')
        lines += ['', '| 测试步骤 | 预期结果 |', '| --- | --- |', f'| {md(case.get("测试步骤"))} | {md(case.get("预期结果"))} |', '']
    return '\n'.join(lines) + '\n'


def init_library(name, parent_folder=None, dry_run=False):
    if not name:
        raise ValueError('name required')
    resources = []
    result = dict(status='ok', resources=resources)
    if dry_run:
        return dict(status='ok', dry_run=True, operations=[io.create_folder(name, parent_folder, dry_run=True), io.create_folder('原件', '<root>', dry_run=True), io.create_folder('正文', '<root>', dry_run=True), io.create_base(name, '<root>', dry_run=True), *[io.create_table('<base>', TABLE_NAMES[k], fields, dry_run=True) for k, fields in STANDARD_TABLES.items()]])
    try:
        root = io.create_folder(name, parent_folder); resources.append(dict(step='root', response=root))
        folders = {'root': get_id(root, 'token', 'folder_token')}
        for key, title in [('originals', '原件'), ('bodies', '正文')]:
            value = io.create_folder(title, folders['root']); resources.append(dict(step=key, response=value)); folders[key] = get_id(value, 'token', 'folder_token')
        value = io.create_base(name, folders['root']); resources.append(dict(step='base', response=value)); base = get_id(value, 'base_token', 'app_token', 'token'); base_response = value
        tables = {}
        for key, fields in STANDARD_TABLES.items():
            value = io.create_table(base, TABLE_NAMES[key], fields); resources.append(dict(step=key, response=value)); tables[key] = get_id(value, 'table_id', 'id')
        lib = dict(name=name, kind='standard', base_token=base, tables=tables, folders=folders)
        try:
            lib['url'] = get_id(base_response, 'url')
        except io.LarkError:
            pass
        actual = schema(lib)
        mismatch = schema_mismatch(actual)
        if mismatch:
            raise io.LarkError('schema_readback', 'missing created fields', detail=mismatch)
        result['config'] = dict(config_version='2.0', libraries=[lib], doc_scopes=[])
    except io.LarkError as exc:
        result.update(status='partial' if resources else 'failed', error=exc.as_dict())
    return result


def is_indexed(lib):
    return lib.get('schema_profile', 'standard') == 'indexed'


def inspect_library(config, library):
    lib = library_config(config, library)
    if is_indexed(lib):
        import indexed
        return indexed.inspect(lib)
    actual = schema(lib)
    rows = read_all(lib)
    return dict(status='ok', fields=actual, missing_fields={k: sorted(field_names(STANDARD_TABLES[k]) - field_names(actual[k])) for k in TABLE_NAMES}, schema_mismatch=schema_mismatch(actual), categories=rows['catalog'], cases_count=len(rows['cases']), complete=True)


def require_batch(result):
    if result['status'] != 'ok':
        raise io.LarkError('partial_write', 'batch write failed; read back before retry', detail=result)
    return result


def run_import(config, plan, *, dry_run=False):
    lib = library_config(config, plan.get('library'))
    if is_indexed(lib):
        import indexed
        return indexed.run_import(lib, plan, dry_run=dry_run)
    report = dict(status='failed', steps=[], operations=[], readback={}, links=dict(base=lib.get('url'), base_token=lib['base_token'], tables=lib['tables']), dry_run=dry_run)
    base, tables = lib['base_token'], lib['tables']
    wrote = False
    batch_row, batch_record_id = None, None
    body_attempts = []
    try:
        report['active_step'] = 'read schema'
        fields = schema(lib)
        missing = schema_mismatch(fields)
        if missing:
            raise ValueError('missing standard fields: ' + json_text(missing))
        rows = read_all(lib)
        fields['existing_categories'] = [r['fields'].get('分类路径') for r in rows['catalog']]
        errors = validate_plan(plan, fields)
        if errors:
            raise ValueError('invalid plan: ' + json_text(errors))
        source = Path(plan['batch']['source_file'])
        raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        # Set source name before comparison, and preserve original filename separately.
        prepared = copy.deepcopy(plan)
        for case in prepared['cases']:
            case.setdefault('来源文件', source.name)
        operations = plan_operations(prepared, rows['cases'])
        report.update(operations=operations['operations'], counts=operations['counts'])
        if operations['counts']['conflict']:
            report['error'] = dict(code='conflict', message='conflicts found; no writes'); return report
        catalog = {r['fields']['分类路径']: r for r in rows['catalog']}
        if len(catalog) != len(rows['catalog']):
            raise ValueError('duplicate categories; Agent must resolve before importing')
        if dry_run:
            planned = [io.upload_file(source, lib['folders']['originals'], dry_run=True)]
            new_categories = [dict(c, 用例数=0, 更新时间='<time>') for c in plan['categories'] if c['分类路径'] not in catalog]
            if new_categories:
                planned.append(io.batch_create(base, tables['catalog'], new_categories, dry_run=True))
            create_preview, update_preview = [], []
            for operation in report['operations']:
                if operation['action'] not in ('create', 'update') or not plan['targets']['base']:
                    continue
                value = to_remote(dict(operation['case'], 来源文件=source.name + ' <original-url>', 入库批次='<batch>', 内容哈希=operation['content_hash'], 入库时间='<time>'))
                if operation['action'] == 'create':
                    create_preview.append(value)
                else:
                    update_preview.append(dict(record_id=operation['record_id'], fields=value))
            if create_preview:
                planned.append(io.batch_create(base, tables['cases'], create_preview, dry_run=True))
            if update_preview:
                planned.append(io.batch_update(base, tables['cases'], update_preview, dry_run=True))
            if plan['targets']['doc']:
                planned.append(dict(command=['lark-cli', 'docs', '+create', '--content', '-', '--doc-format', 'markdown', '--as', 'user', '--dry-run'], scope='all effective cases in affected categories'))
            report.update(status='ok', steps=['upload/reuse source', 'create categories', 'write cases' if plan['targets']['base'] else 'document only', 'create category bodies' if plan['targets']['doc'] else 'no bodies', 'update counts', 'write batch', 'readback'], planned_data=prepared, commands=planned)
            return report
        batch_id = 'batch-' + uuid.uuid4().hex
        report['batch_id'] = batch_id
        timestamp = now()
        originals = [r['fields'].get('原件链接') for r in rows['batches'] if r['fields'].get('原件SHA256') == raw_hash and r['fields'].get('原件链接')]
        report['active_step'] = 'upload original'
        source_url = originals[0] if originals else get_url(io.upload_file(source, lib['folders']['originals']), 'file')
        wrote = not bool(originals)
        report['links']['original'] = source_url
        prior_titles = set()
        for record in rows['batches']:
            if record['fields'].get('原件SHA256') != raw_hash:
                continue
            try:
                previous = json.loads(record['fields'].get('说明') or '{}')
                if isinstance(previous, dict):
                    prior_titles.update(a['title'] for a in previous.get('body_attempts', []) if not a.get('confirmed') and not a.get('failed_known'))
            except (ValueError, KeyError, TypeError):
                pass
        batch_row = dict(批次号=batch_id, 来源文件=source.name, 原件链接=source_url, 原件SHA256=raw_hash,
                         新增数=0, 更新数=0, 未变数=operations['counts']['unchanged'],
                         冲突数=operations['counts']['conflict'] + operations['counts']['skip'],
                         导入时间=timestamp, 执行结果='部分完成', 说明=json_text(dict(agent_note=plan['batch'].get('note', ''), body_attempts=body_attempts)))
        # Persist source identity before case writes. Unknown creation is read back,
        # never re-created in this invocation.
        report['active_step'] = 'register batch'
        wrote = True
        require_batch(io.batch_create(base, tables['batches'], [batch_row]))
        batches = io.list_records(base, tables['batches'])
        matching = [r for r in batches['records'] if r['fields'].get('批次号') == batch_id]
        if not batches['complete'] or len(matching) != 1:
            raise io.LarkError('batch_readback', 'batch registration unconfirmed; no case writes', detail=batches)
        batch_record_id = matching[0]['record_id']
        report['batch_record_id'] = batch_record_id
        report['active_step'] = 'write categories'
        for category in plan['categories']:
            if category['分类路径'] not in catalog:
                wrote = True; require_batch(io.batch_create(base, tables['catalog'], [dict(category, 用例数=0, 更新时间=timestamp)]))
        report['steps'].append('categories')
        current = {r['fields']['用例编号']: dict(record_id=r['record_id'], fields=normalized(r['fields'])) for r in rows['cases']}
        create_rows, updates, expected = [], [], {}
        for operation in report['operations']:
            if operation['action'] not in ('create', 'update') or not plan['targets']['base']:
                continue
            case = dict(operation['case'], 来源文件=source.name + ' ' + source_url, 入库批次=batch_id, 内容哈希=operation['content_hash'], 入库时间=timestamp)
            # An update is a newly imported version requiring review again.
            case['审核状态'] = operation['case'].get('审核状态', '待审核')
            expected[case['用例编号']] = case
            if operation['action'] == 'create':
                create_rows.append(to_remote(case))
            else:
                updates.append(dict(record_id=operation['record_id'], fields=to_remote(case)))
        report['active_step'] = 'write cases'
        batch_row['新增数'], batch_row['更新数'] = len(create_rows), len(updates)
        if create_rows:
            wrote = True; require_batch(io.batch_create(base, tables['cases'], create_rows))
        if updates:
            wrote = True; require_batch(io.batch_update(base, tables['cases'], updates))
        report['steps'].append('cases')
        # Complete read after writes resolves actual IDs; never infer batch IDs.
        after = read_all(lib)
        readback_errors = []
        indexed = {}
        for record in after['cases']:
            key = record['fields'].get('用例编号')
            if key in indexed:
                readback_errors.append(dict(case_id=key, error='duplicate id after write'))
            indexed[key] = record
        for key, wanted in expected.items():
            actual = indexed.get(key)
            if not actual or actual['fields'].get('入库批次') != batch_id or actual['fields'].get('内容哈希') != content_hash(wanted) or content_hash(actual['fields']) != content_hash(wanted):
                readback_errors.append(dict(case_id=key, error='case readback mismatch'))
        if sum(r['fields'].get('入库批次') == batch_id for r in after['cases']) != len(expected):
            readback_errors.append(dict(error='batch record count mismatch'))
        if readback_errors:
            report.update(status='partial', readback=dict(complete=True, errors=readback_errors)); return report
        catalog = {r['fields']['分类路径']: r for r in after['catalog']}
        categories = {r['fields'].get('分类路径'): r['fields'] for r in after['catalog']}
        categories.update({c['分类路径']: c for c in plan['categories']})
        # Only changed categories or incomplete previous imports need body repair.
        changed = {o['case']['所属分类'] for o in report['operations'] if o['action'] in ('create', 'update')}
        changed.update(r['fields'].get('所属分类') for r in rows['cases'] if r['fields'].get('用例编号') in expected)
        candidates = changed | {c['所属分类'] for c in plan['cases']} | {c['分类路径'] for c in plan['categories']}
        case_updates, category_updates, documents = [], [], {}
        report['resources'] = []
        report['active_step'] = 'render bodies and counts'
        for category_path in sorted(candidates):
            all_records = [r for r in after['cases'] if r['fields'].get('所属分类') == category_path]
            all_cases = [normalized(r['fields']) for r in all_records if normalized(r['fields'])['状态'] == '有效']
            render_cases = all_cases
            if not plan['targets']['base']:
                joined = {c['用例编号']: c for c in all_cases}
                joined.update({o['case_id']: o['case'] for o in report['operations'] if o['case']['所属分类'] == category_path and o['action'] not in ('skip', 'conflict')})
                render_cases = [c for c in joined.values() if c['状态'] == '有效']
            existing = catalog[category_path]['fields']
            url = doc_ref(existing.get('正文文档', ''))
            metadata = {k: categories[category_path][k] for k in ('分类说明', '关键词') if k in categories[category_path]}
            meta_changed = any(existing.get(k) != v for k, v in metadata.items())
            stale_links = bool(url and any(doc_ref(r['fields'].get('正文文档', '')) != url for r in all_records))
            count_changed = existing.get('用例数') != len(all_cases)
            document_changed = category_path in changed or meta_changed or not plan['targets']['base']
            needs_body = plan['targets']['doc'] and (document_changed or not url)
            update = dict(metadata, 用例数=len(all_cases), 更新时间=timestamp)
            if needs_body:
                wrote = True
                title = lib['name'] + ' ' + category_path
                markdown = render_category_markdown(categories[category_path], render_cases, lib['name'])
                io.split_markdown(markdown)
                if url:
                    resource = dict(category=category_path, url=url, action='update')
                    report['resources'].append(resource)
                    result = io.update_doc(url, markdown)
                    resource['chunks'] = result.get('chunks')
                else:
                    attempt = dict(title=title, confirmed=False)
                    body_attempts.append(attempt)
                    batch_row['说明'] = json_text(dict(agent_note=plan['batch'].get('note', ''), body_attempts=body_attempts))
                    require_batch(io.batch_update(base, tables['batches'], [dict(record_id=batch_record_id, fields=batch_row)]))
                    created = io.create_doc(title, markdown, lib['folders']['bodies'], allow_create=title not in prior_titles)
                    url = get_url(created, 'docx'); attempt['confirmed'] = True
                    report['resources'].append(dict(category=category_path, url=url, action='create_or_recover', resources=created.get('resources', []), superseded=created.get('superseded', []), chunks=created.get('body_upload', {}).get('chunks')))
                update['正文文档'] = url
            if plan['targets']['doc'] and url:
                documents[category_path] = url
                if needs_body or stale_links:
                    for record in all_records:
                        if doc_ref(record['fields'].get('正文文档', '')) != url:
                            case_updates.append(dict(record_id=record['record_id'], fields={'正文文档': url}))
            if count_changed or meta_changed or needs_body:
                category_updates.append(dict(record_id=catalog[category_path]['record_id'], fields=update))
        report['links']['bodies'] = documents
        if case_updates:
            wrote = True; require_batch(io.batch_update(base, tables['cases'], case_updates))
        if category_updates:
            wrote = True; require_batch(io.batch_update(base, tables['catalog'], category_updates))
        report['links']['bodies'] = documents
        for category_path, url in documents.items():
            fetched = io.fetch_doc(doc_ref(url))
            if not fetched.get('content'):
                readback_errors.append(dict(category=category_path, error='body unreadable'))
        batch_row['执行结果'] = '部分完成' if readback_errors else '完成'
        batch_row['说明'] = json_text(dict(agent_note=plan['batch'].get('note', ''), body_attempts=body_attempts))
        report['active_step'] = 'finalize batch'
        require_batch(io.batch_update(base, tables['batches'], [dict(record_id=batch_record_id, fields=batch_row)]))
        report['active_step'] = 'final readback'
        final = read_all(lib)
        found_batches = [r for r in final['batches'] if r['fields'].get('批次号') == batch_id]
        if len(found_batches) != 1 or any(found_batches[0]['fields'].get(k) != v for k, v in batch_row.items()):
            readback_errors.append(dict(error='batch readback mismatch'))
        final_catalog = {r['record_id']: r['fields'] for r in final['catalog']}
        final_cases = {r['record_id']: r['fields'] for r in final['cases']}
        for update in category_updates + case_updates:
            target = final_catalog if update in category_updates else final_cases
            if any(target.get(update['record_id'], {}).get(k) != v for k, v in update['fields'].items()):
                readback_errors.append(dict(record_id=update['record_id'], error='links/count readback mismatch'))
        for key, wanted in expected.items():
            actual = [r['fields'] for r in final['cases'] if r['fields'].get('用例编号') == key]
            if len(actual) != 1 or content_hash(actual[0]) != content_hash(wanted):
                readback_errors.append(dict(case_id=key, error='final content changed'))
        report.update(status='partial' if readback_errors else 'ok', readback=dict(complete=True, expected_cases=len(expected), errors=readback_errors))
        report['steps'].extend(['bodies', 'catalog counts', 'batch', 'readback'])
        report.pop('active_step', None)
    except io.LarkError as exc:
        if body_attempts and not body_attempts[-1]['confirmed'] and not io._unknown_write(exc) and exc.code != 'document_unknown' and not (isinstance(exc.detail, dict) and (exc.detail.get('unknown_outcome') or exc.detail.get('resources'))):
            body_attempts[-1]['failed_known'] = True
        if isinstance(exc.detail, dict) and exc.detail.get('resources'):
            report.setdefault('resources', []).extend(exc.detail['resources'])
        report.update(status='partial' if wrote else 'failed', error=exc.as_dict())
        if exc.code == 'document_unknown':
            report['next_steps'] = dict(
                action='verify_missing_body_then_resolve_batch_attempts',
                source_sha256=raw_hash, title=body_attempts[-1]['title'] if body_attempts else None,
                instructions='Follow references/import-plan.md: export a backup, completely list the body folder, verify permissions and delayed results, then with explicit user confirmation mark every unresolved matching title/source SHA attempt failed_known=true, preserving other fields and recording evidence. Read back the changes before dry-run and import. If a document exists, reuse it; do not clear the block.')
        if exc.detail and isinstance(exc.detail, dict) and exc.detail.get('unknown_outcome'):
            # Read-only diagnosis, never repeat the failed creation.
            try:
                report['unknown_outcome_readback'] = read_all(lib)
            except io.LarkError as probe:
                report['unknown_outcome_readback'] = probe.as_dict()
    except (ValueError, OSError, TypeError) as exc:
        report.update(status='partial' if wrote else 'failed', input_error=str(exc))
    finally:
        if batch_row and not dry_run:
            # Every error/early partial return retains source identity and audit linkage.
            try:
                if batch_record_id is None:
                    probe = io.list_records(base, tables['batches'])
                    found = [r for r in probe['records'] if r['fields'].get('批次号') == batch_row['批次号']]
                    if not probe['complete'] or len(found) != 1:
                        raise io.LarkError('batch_unconfirmed', 'batch row could not be confirmed', detail=probe)
                    batch_record_id = found[0]['record_id']
                if report['status'] != 'ok':
                    batch_row['执行结果'] = '部分完成' if wrote else '失败'
                    batch_row['说明'] = json_text(dict(agent_note=plan['batch'].get('note', ''), body_attempts=body_attempts, error=report.get('error', report.get('input_error', report.get('readback')))))
                    require_batch(io.batch_update(base, tables['batches'], [dict(record_id=batch_record_id, fields=batch_row)]))
                probe = io.list_records(base, tables['batches'])
                found = [r for r in probe['records'] if r['record_id'] == batch_record_id]
                if not probe['complete'] or len(found) != 1 or any(found[0]['fields'].get(k) != v for k, v in batch_row.items()):
                    raise io.LarkError('batch_readback', 'final batch state not confirmed', detail=probe)
                report['batch_audit'] = dict(confirmed=True, record_id=batch_record_id, result=batch_row['执行结果'])
            except io.LarkError as audit_error:
                report['batch_audit'] = dict(confirmed=False, error=audit_error.as_dict())
                if report['status'] == 'ok':
                    report['status'] = 'partial'
    return report


def check_library(config, library):
    lib = library_config(config, library)
    if is_indexed(lib):
        import indexed
        return indexed.check(lib)
    rows = read_all(lib)
    issues, seen = [], set()
    paths = {r['fields'].get('分类路径'): r for r in rows['catalog']}
    for record in rows['cases']:
        case = record['fields']; key = case.get('用例编号')
        if key in seen:
            issues.append(dict(record_id=record['record_id'], issue='duplicate case id'))
        seen.add(key)
        for field in ('用例编号', '用例标题', '所属分类', '来源文件', '来源位置', '入库批次', '内容哈希', '入库时间'):
            if not case.get(field):
                issues.append(dict(record_id=record['record_id'], issue='empty ' + field))
        if not case.get('测试步骤') and not case.get('预期结果'):
            issues.append(dict(record_id=record['record_id'], issue='empty steps and expectation'))
        if case.get('所属分类') not in paths:
            issues.append(dict(record_id=record['record_id'], issue='missing category'))
        if not case.get('正文文档'):
            issues.append(dict(record_id=record['record_id'], issue='missing body link'))
        try:
            if case.get('内容哈希') != content_hash(case):
                issues.append(dict(record_id=record['record_id'], issue='content changed; Agent review required'))
        except (ValueError, TypeError):
            issues.append(dict(record_id=record['record_id'], issue='invalid extension JSON'))
    for path, record in paths.items():
        actual = sum(r['fields'].get('所属分类') == path and r['fields'].get('状态', ['有效']) in ('有效', ['有效'], None, '') for r in rows['cases'])
        if record['fields'].get('用例数') != actual:
            issues.append(dict(category=path, issue='category count mismatch', actual=actual))
    if len(paths) != len(rows['catalog']):
        issues.append(dict(issue='duplicate category paths'))
    return dict(status='ok', complete=True, issues=issues, cases_count=len(rows['cases']))


def export_library(config, library, out_dir):
    lib = library_config(config, library)
    if is_indexed(lib):
        import indexed
        return indexed.export(lib, out_dir)
    directory = Path(out_dir); directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(status='ok', library=library, files=[], errors=[], complete=True)
    for key, table in lib['tables'].items():
        if key not in TABLE_NAMES:
            continue
        response = io.list_records(lib['base_token'], table)
        path = directory / (key + '.ndjson')
        path.write_text(''.join(json_text(r) + '\n' for r in response['records']), encoding='utf-8')
        manifest['files'].append(dict(path=path.name, count=len(response['records']), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), complete=response['complete']))
        if not response['complete']:
            manifest['errors'].append(dict(table=key, error=response['error']))
        if key == 'catalog':
            for index, record in enumerate(response['records']):
                url = record['fields'].get('正文文档')
                if not url:
                    continue
                try:
                    doc = io.fetch_doc(doc_ref(url))
                    if not doc.get('content'):
                        raise io.LarkError('empty_body', 'document content empty')
                    body = directory / f'body-{index + 1:04d}.md'; body.write_text(doc['content'], encoding='utf-8')
                    manifest['files'].append(dict(path=body.name, url=url, sha256=hashlib.sha256(body.read_bytes()).hexdigest(), complete=True))
                except io.LarkError as exc:
                    manifest['errors'].append(dict(url=url, error=exc.as_dict()))
    if manifest['errors']:
        manifest.update(status='partial', complete=False)
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def publish_library(config, library, selection=None, *, dry_run=False):
    lib = library_config(config, library)
    if not is_indexed(lib):
        raise ValueError('publish applies to schema_profile=indexed; standard imports are already the library of record')
    import indexed
    return indexed.publish(lib, selection, dry_run=dry_run)


def browse_view(config, library, *, dry_run=False):
    lib = library_config(config, library)
    if not is_indexed(lib):
        raise ValueError('browse-view applies to schema_profile=indexed')
    import indexed
    return indexed.manage_browse_view(lib, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init-library', 'inspect-library', 'import', 'check', 'export', 'publish', 'browse-view'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--out-dir' if name == 'export' else '--out', required=True)
        cmd.add_argument('--overwrite', action='store_true')
        if name == 'init-library':
            cmd.add_argument('--name', required=True); cmd.add_argument('--parent-folder'); cmd.add_argument('--dry-run', action='store_true')
        else:
            cmd.add_argument('--config', required=True)
            if name == 'import':
                cmd.add_argument('--plan', required=True); cmd.add_argument('--dry-run', action='store_true')
            else:
                cmd.add_argument('--library', required=True)
            if name == 'publish':
                cmd.add_argument('--plan'); cmd.add_argument('--dry-run', action='store_true')
            if name == 'browse-view':
                cmd.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        inputs = [getattr(args, n) for n in ('config', 'plan') if getattr(args, n, None)]
        config = json.loads(Path(args.config).read_text()) if hasattr(args, 'config') else None
        plan = json.loads(Path(args.plan).read_text()) if getattr(args, 'plan', None) else None
        sources = [plan.get('batch', {}).get('source_file', '')] if plan and args.command == 'import' else []
        inputs.extend(sources)
        target = io.safe_output(args.out_dir if args.command == 'export' else args.out, inputs, sources=sources, overwrite=args.overwrite, directory=args.command == 'export')
        if args.command == 'export' and target.exists():
            # Only known export names are overwritten; refuse foreign contents.
            if any(not re.fullmatch(r'(catalog|cases|batches|sources|review|issues|routes)\.ndjson|manifest\.json|body-\d{4}\.(md|xml)', p.name) for p in target.iterdir()):
                raise ValueError('export directory contains unrelated files')
        if args.command == 'init-library':
            result = init_library(args.name, args.parent_folder, args.dry_run)
        elif args.command == 'import':
            result = run_import(config, plan, dry_run=args.dry_run)
        elif args.command == 'inspect-library':
            result = inspect_library(config, args.library)
        elif args.command == 'check':
            result = check_library(config, args.library)
        elif args.command == 'publish':
            result = publish_library(config, args.library, plan, dry_run=args.dry_run)
        elif args.command == 'browse-view':
            result = browse_view(config, args.library, dry_run=args.dry_run)
        else:
            result = export_library(config, args.library, target)
        if args.command != 'export':
            target.parent.mkdir(parents=True, exist_ok=True)
            output = result['config'] if args.command == 'init-library' and result.get('status') == 'ok' and not args.dry_run else result
            target.write_text(json.dumps(io.redact(output), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(dict(status=result['status'], out=str(target), counts=result.get('counts', {})), ensure_ascii=False))
        return 2 if result.get('input_error') else 0 if result['status'] == 'ok' else 3
    except (ValueError, OSError, TypeError, io.LarkError) as exc:
        print(json.dumps(dict(status='failed', error=exc.as_dict() if isinstance(exc, io.LarkError) else str(exc)), ensure_ascii=False)); return 3 if isinstance(exc, io.LarkError) else 2


if __name__ == '__main__':
    raise SystemExit(main())
