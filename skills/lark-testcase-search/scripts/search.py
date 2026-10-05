"""Read-only recall and mechanical rendering; judgments belong to the Agent."""
import argparse
import json
import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path
import sys

import lark_io as io

STANDARD_FIELDS = ['用例编号', '用例标题', '摘要', '关键词', '需求编号', '测试步骤', '预期结果', '所属分类']
LIB_MATCH = ['高', '中', '低', '不相关', '未判定']
CASE_MATCH = ['完全匹配', '高', '中', '低', '不相关', '未判定']
REUSE = ['直接复用', '修改后复用', '仅参考', '不建议', '未判定']


def validate_config(config):
    if not isinstance(config, dict) or config.get('config_version') != '2.0':
        raise ValueError('config_version must be 2.0')
    if not isinstance(config.get('libraries'), list) or not isinstance(config.get('doc_scopes', []), list):
        raise ValueError('libraries and doc_scopes must be arrays')
    names = set()
    for lib in config['libraries']:
        if not isinstance(lib, dict) or not all(isinstance(lib.get(k), str) and lib[k] for k in ('name', 'base_token', 'kind')):
            raise ValueError('library requires name, kind, base_token')
        if lib['name'] in names or lib['kind'] not in ('standard', 'external'):
            raise ValueError('duplicate name or invalid kind')
        names.add(lib['name'])
        if 'url' in lib:
            url = lib['url']
            if not isinstance(url, str):
                raise ValueError('library url must be an absolute HTTP(S) URL')
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('library url must be an absolute HTTP(S) URL without credentials')
        if not isinstance(lib.get('tables'), dict) or not isinstance(lib['tables'].get('cases'), str) or not lib['tables']['cases']:
            raise ValueError('library requires tables.cases')
    for scope in config.get('doc_scopes', []):
        if not isinstance(scope, dict) or not all(isinstance(scope.get(k), str) and scope[k] for k in ('name', 'folder_token')):
            raise ValueError('doc scope requires name and folder_token')
    return config


def selected(config, names):
    validate_config(config)
    by_name = {lib['name']: lib for lib in config['libraries']}
    if not names or any(name not in by_name for name in names):
        raise ValueError('select configured library names explicitly')
    return [by_name[name] for name in dict.fromkeys(names)]


def status_for(items):
    complete = all(item.get('complete', True) for item in items)
    return 'ok' if complete else 'partial'


def array_payload(payload, *keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise io.LarkError('response_shape', 'missing expected array')


def list_folder(folder):
    files, token, seen, pages = [], None, set(), 0
    try:
        while True:
            args = ['drive', 'files', 'list', '--folder-token', folder, '--page-size', '200']
            if token:
                args += ['--page-token', token]
            payload = io.data(io.run_lark(args))
            files.extend(array_payload(payload, 'files', 'items'))
            pages += 1
            if type(payload.get('has_more')) is not bool:
                raise io.LarkError('response_shape', 'folder has_more missing')
            if not payload['has_more']:
                return {'files': files, 'complete': True, 'pages': pages, 'error': None}
            token = payload.get('next_page_token', payload.get('page_token'))
            if not token or token in seen:
                raise io.LarkError('pagination', 'folder cursor missing or repeated')
            seen.add(token)
    except io.LarkError as exc:
        return {'files': files, 'complete': False, 'pages': pages, 'error': exc.as_dict()}


def list_libraries(config):
    validate_config(config)
    libs, scopes = [], []
    for lib in config['libraries']:
        item = {'name': lib['name'], 'kind': lib['kind'], 'base_token': lib['base_token'], 'complete': True, 'errors': []}
        try:
            item['tables'] = array_payload(io.list_tables(lib['base_token']), 'tables', 'items')
            item['fields'] = {}
            for table in item['tables']:
                tid = table.get('table_id', table.get('id', table.get('name')))
                try:
                    item['fields'][tid] = array_payload(io.list_fields(lib['base_token'], tid), 'fields', 'items')
                except io.LarkError as exc:
                    item['complete'] = False
                    item['errors'].append({'table': tid, 'error': exc.as_dict()})
            if lib['tables'].get('catalog'):
                item['catalog'] = io.list_records(lib['base_token'], lib['tables']['catalog'])
                item['complete'] &= item['catalog']['complete']
            # Count actual records rather than interpreting library text.
            records = io.list_records(lib['base_token'], lib['tables']['cases'])
            item['case_count'] = len(records['records'])
            item['count_complete'] = records['complete']
            item['complete'] &= records['complete']
            if records.get('error'):
                item['errors'].append({'table': lib['tables']['cases'], 'error': records['error']})
        except io.LarkError as exc:
            item['complete'] = False
            item['errors'].append(exc.as_dict())
        libs.append(item)
    for scope in config.get('doc_scopes', []):
        try:
            listing = list_folder(scope['folder_token'])
            scopes.append({**scope, **listing})
        except io.LarkError as exc:
            scopes.append({**scope, 'complete': False, 'error': exc.as_dict(), 'files': []})
    return {'status': status_for(libs + scopes), 'libraries': libs, 'doc_scopes': scopes}


def record_locator(lib, record_id):
    locator = {'base_token': lib['base_token'], 'table_id': lib['tables']['cases'], 'record_id': record_id}
    url = None
    if lib.get('url'):
        parsed = urlsplit(lib['url'])
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in ('table', 'record')]
        query += [('table', locator['table_id']), ('record', record_id)]
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return {'url': url, 'locator': locator}


def find_cases(config, libraries, keywords, *, search_fields=None, limit_per_keyword=200):
    libs = selected(config, libraries)
    if not keywords or any(not isinstance(k, str) or not k.strip() for k in keywords):
        raise ValueError('keywords must be nonempty strings')
    if not 1 <= limit_per_keyword <= 200:
        raise ValueError('limit_per_keyword is page size, range 1..200')
    if search_fields is not None and (not search_fields or len(search_fields) > 20 or any(not isinstance(f, str) or not f for f in search_fields)):
        raise ValueError('search_fields must contain 1..20 nonempty fields')
    candidates, runs = {}, []
    for lib in libs:
        fields, omitted = search_fields, []
        selection_rule = 'explicit_search_fields_in_supplied_order'
        try:
            if fields is None:
                if lib['kind'] == 'standard':
                    fields = STANDARD_FIELDS
                    selection_rule = 'standard_eight_text_fields'
                else:
                    schema = array_payload(io.list_fields(lib['base_token'], lib['tables']['cases']), 'fields', 'items')
                    text_fields = [f.get('name', f.get('field_name')) for f in schema if f.get('type') in ('text', 1)]
                    text_fields = sorted(f for f in text_fields if f)
                    fields, omitted = text_fields[:20], text_fields[20:]
                    selection_rule = 'external_text_field_names_unicode_ascending_first_20'
            if not fields:
                raise ValueError('no text search fields; Agent must specify fields')
            for keyword in dict.fromkeys(keywords):
                result = io.search_records(lib['base_token'], lib['tables']['cases'], keyword, fields, limit=limit_per_keyword)
                runs.append({'library': lib['name'], 'table': lib['tables']['cases'], 'keyword': keyword, 'search_fields': fields,
                             'omitted_fields': omitted, 'selection_rule': selection_rule, 'field_scope_complete': not omitted,
                             'complete': result['complete'], 'pages': result['pages'], 'error': result.get('error')})
                for record in result['records']:
                    rid = record['record_id']
                    key = (lib['name'], rid)
                    if key not in candidates:
                        candidates[key] = {**record, 'library': lib['name'], 'table': lib['tables']['cases'], 'keywords': [],
                                           **record_locator(lib, rid)}
                    elif candidates[key]['fields'] != record['fields']:
                        variants = candidates[key].setdefault('observed_field_variants', [])
                        if record['fields'] not in variants:
                            variants.append(record['fields'])
                    candidates[key]['keywords'].append(keyword)
        except (io.LarkError, ValueError) as exc:
            runs.append({'library': lib['name'], 'complete': False, 'error': exc.as_dict() if isinstance(exc, io.LarkError) else {'message': str(exc)}})
    remote_complete = all(r['complete'] for r in runs)
    return {'status': 'ok' if remote_complete else 'partial', 'remote_complete': remote_complete,
            'field_scope_complete': all(r.get('field_scope_complete', False) for r in runs),
            'candidates': list(candidates.values()), 'searches': runs}


def dump_cases(config, library, out_path, *, overwrite=False):
    out_path = check_output(out_path, overwrite=overwrite)
    lib = selected(config, [library])[0]
    result = io.list_records(lib['base_token'], lib['tables']['cases'])
    Path(out_path).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in result['records']), encoding='utf-8')
    return {'status': 'ok' if result['complete'] else 'partial', 'library': library, 'count': len(result['records']),
            'complete': result['complete'], 'pages': result['pages'], 'error': result.get('error'), 'out': str(out_path)}


def search_feishu_docs(query, *, max_pages=5):
    if not query.strip() or max_pages < 1:
        raise ValueError('query must be nonempty and max_pages positive')
    result = io.search_docs(query, max_pages=max_pages)
    items = []
    for item in result['items']:
        meta = item.get('result_meta') or {}
        title = item.get('title') or item.get('title_highlighted')
        summary = item.get('summary') or item.get('summary_highlighted')
        items.append({'title': strip_highlight(title), 'type': meta.get('doc_types') or item.get('type') or item.get('entity_type'),
                      'token': meta.get('token') or item.get('token'), 'url': meta.get('url') or item.get('url'),
                      'summary': strip_highlight(summary), 'raw': item})
    return {**result, 'status': 'ok' if result['complete'] else 'partial', 'items': items}


def strip_highlight(value):
    return unescape(re.sub(r'</?h>', '', value, flags=re.I)) if isinstance(value, str) else value


def document_title(result, text):
    for value, source in ((result.get('title'), 'response.title'),
                          ((result.get('document') or {}).get('title'), 'document.title')):
        if isinstance(value, str) and value.strip():
            return value, source
    match = re.match(r'\A\ufeff?\s*<title>(.*?)</title>', text, re.DOTALL)
    if match and match.group(1).strip():
        return unescape(match.group(1)), 'content.title'
    return None, 'unavailable'


def read_feishu_doc(doc, out_path, *, overwrite=False):
    out_path = check_output(out_path, overwrite=overwrite)
    result = io.fetch_doc(doc)
    text = result.get('content', result.get('markdown', result.get('text')))
    if not isinstance(text, str):
        raise ValueError('fetch did not return document content')
    title, title_source = document_title(result, text)
    Path(out_path).write_text(text, encoding='utf-8')
    return {'status': 'ok', 'doc': doc, 'title': title, 'title_source': title_source, 'revision': result.get('revision'),
            'length': len(text), 'out': str(out_path), 'content_is_untrusted_data': True}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_result(result):
    if not isinstance(result, dict) or not all(k in result for k in ('query', 'interpretation', 'scope', 'libraries', 'cases')):
        raise ValueError('result requires query, interpretation, scope, libraries, cases')
    if not nonempty(result['query']) or not nonempty(result['interpretation']):
        raise ValueError('query and interpretation must be nonempty text')
    scope = result['scope']
    if not isinstance(scope, dict) or not isinstance(scope.get('complete'), bool):
        raise ValueError('scope requires boolean complete')
    for key in ('libraries', 'tables', 'documents', 'keywords', 'failures', 'unfinished'):
        if not isinstance(scope.get(key), list):
            raise ValueError('scope.' + key + ' must be an array')
    for key, choices in (('libraries', LIB_MATCH), ('cases', CASE_MATCH)):
        if not isinstance(result[key], list):
            raise ValueError(key + ' must be an array')
        for item in result[key]:
            if not isinstance(item, dict) or item.get('match') not in choices or not nonempty(item.get('reason')):
                raise ValueError(key + ' requires valid match and nonempty reason')
            if not nonempty(item.get('name' if key == 'libraries' else 'title')):
                raise ValueError(key + ' requires name/title')
            if key == 'cases':
                if not nonempty(item.get('library')) or not nonempty(item.get('locator')) or item.get('reuse') not in REUSE:
                    raise ValueError('case requires library, locator and reuse')
                if not isinstance(item.get('differences'), list):
                    raise ValueError('case differences must be an array')
                evidence = item.get('evidence', [])
                if not isinstance(evidence, list) or any(not isinstance(e, dict) or not nonempty(e.get('quote')) or not nonempty(e.get('source')) for e in evidence):
                    raise ValueError('evidence requires quote and source')
                undecided = item['match'] == '未判定' or item['reuse'] == '未判定'
                if not evidence and not (undecided and nonempty(item.get('undetermined_reason'))):
                    raise ValueError('case requires evidence or reason for undetermined judgment')
                if undecided and not nonempty(item.get('undetermined_reason')):
                    raise ValueError('undetermined judgment requires missing evidence reason')
    return result


def md(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\r', '').replace('\n', '<br>').replace('`', '&#96;')


def render_result(result):
    validate_result(result)
    lines = ['# 用例检索报告', '', '## 检索理解', '', md(result['query']), '', md(result['interpretation']), '', '## 检索范围与完整性', '']
    for key, value in result['scope'].items():
        lines.append(f'- {md(key)}：{md(value)}')
    lines += ['', '## 库级匹配', '', '| 库/分类/文档 | 匹配度 | 理由 |', '| --- | --- | --- |']
    for lib in result['libraries']:
        lines.append('| ' + ' | '.join(md(lib[k]) for k in ('name', 'match', 'reason')) + ' |')
    lines += ['', '## 用例级匹配', '']
    for match in CASE_MATCH:
        cases = [c for c in result['cases'] if c['match'] == match]
        if not cases:
            continue
        lines += ['### ' + match, '', '| 用例 | 库 | 复用建议 | 理由 | 定位 |', '| --- | --- | --- | --- | --- |']
        for case in cases:
            lines.append('| ' + ' | '.join(md(case[k]) for k in ('title', 'library', 'reuse', 'reason', 'locator')) + ' |')
    lines += ['', '## 差异与存疑', '']
    for case in result['cases']:
        lines += ['### ' + md(case['title']), '', '- 差异：' + md(case['differences'])]
        if case.get('undetermined_reason'):
            lines.append('- 未判定原因：' + md(case['undetermined_reason']))
        for evidence in case.get('evidence', []):
            lines.append('- 原文：' + md(evidence['quote']) + '；来源：' + md(evidence['source']))
    lines += ['', '## 未完成范围', '', md(result['scope']['unfinished']), '', md(result['scope']['failures']), '']
    return '\n'.join(lines)


def check_output(path, inputs=(), overwrite=False):
    out = Path(path).resolve()
    for source in inputs:
        src = Path(source).resolve()
        if out == src or out.is_relative_to(src.parent):
            raise ValueError('output must be outside every input source directory')
    if out.exists() and (not overwrite or not out.is_file()):
        raise ValueError('output exists; use --overwrite for an output file')
    if not out.parent.is_dir():
        raise ValueError('output parent directory does not exist')
    return out


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('libraries', 'find', 'dump', 'doc-search', 'doc-read', 'render'):
        p = sub.add_parser(command)
        p.add_argument('--out', required=True)
        p.add_argument('--overwrite', action='store_true')
        if command in ('libraries', 'find', 'dump'):
            p.add_argument('--config', required=True)
        if command in ('find', 'dump'):
            p.add_argument('--library', action='append', required=True)
        if command == 'find':
            p.add_argument('--keyword', action='append', required=True)
            p.add_argument('--search-field', action='append')
            p.add_argument('--limit-per-keyword', type=int, default=200)
        if command == 'doc-search':
            p.add_argument('--query', required=True)
            p.add_argument('--max-pages', type=int, default=5)
        if command == 'doc-read':
            p.add_argument('--doc', required=True)
        if command == 'render':
            p.add_argument('--input', required=True)
    try:
        args = parser.parse_args(argv)
        inputs = [getattr(args, k) for k in ('config', 'input') if hasattr(args, k)]
        out = check_output(args.out, inputs, args.overwrite)
        config = validate_config(json.loads(Path(args.config).read_text(encoding='utf-8'))) if hasattr(args, 'config') else None
        if args.command == 'libraries':
            result = list_libraries(config)
        elif args.command == 'find':
            result = find_cases(config, args.library, args.keyword, search_fields=args.search_field, limit_per_keyword=args.limit_per_keyword)
        elif args.command == 'dump':
            if len(args.library) != 1:
                raise ValueError('dump requires exactly one library')
            result = dump_cases(config, args.library[0], out, overwrite=args.overwrite)
        elif args.command == 'doc-search':
            result = search_feishu_docs(args.query, max_pages=args.max_pages)
        elif args.command == 'doc-read':
            result = read_feishu_doc(args.doc, out, overwrite=args.overwrite)
        else:
            content = render_result(json.loads(Path(args.input).read_text(encoding='utf-8')))
            out.write_text(content, encoding='utf-8')
            result = {'status': 'ok'}
        if args.command not in ('dump', 'doc-read', 'render'):
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        summary = {'status': result['status'], 'out': str(out)}
        for key in ('count', 'complete', 'pages', 'length', 'title', 'title_source', 'revision', 'doc', 'error', 'remote_complete', 'field_scope_complete'):
            if key in result:
                summary[key] = result[key]
        for key in ('candidates', 'libraries', 'documents', 'items'):
            if isinstance(result.get(key), list):
                summary[key + '_count'] = len(result[key])
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if result['status'] == 'ok' else 3
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 2
    except io.LarkError as exc:
        print(json.dumps({'status': 'failed', 'error': exc.as_dict()}, ensure_ascii=False))
        return 3


if __name__ == '__main__':
    sys.exit(main())
