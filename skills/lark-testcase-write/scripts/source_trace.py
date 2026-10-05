"""Optional structured sources: render what the Agent recorded, never infer it.

A case's 参考来源 may be a legacy string, a legacy/structured object or a list
of them. document.sources and document.navigation are optional. Missing
IDs, versions and links are shown as missing; no value is constructed here.
"""
import re
from urllib.parse import urlsplit, parse_qsl
import lark_io

URL_KEYS = ('url', 'index_url', 'body_url')
SCALAR_KEYS = ('kind', 'type', 'title', 'section', 'system_id', 'case_no', 'content_version',
               'reason', 'note', 'ref', 'path')
KNOWN_KEYS = set(URL_KEYS) | set(SCALAR_KEYS) | {'unconfirmed'}
KIND_LABELS = {'requirement': '需求来源', 'reference_case': '参考用例', 'search_report': '检索报告'}
# Fields reported as missing (not errors) per kind; others are not expected.
EXPECTED = {'requirement': ('url', 'section'),
            'reference_case': ('case_no', 'system_id', 'content_version', 'index_url', 'body_url', 'reason')}
LABELS = {'kind': '类别', 'type': '复用方式', 'title': '标题', 'case_no': '可读编号',
          'content_version': '内容版本', 'url': '链接', 'section': '章节/范围',
          'index_url': '索引记录链接', 'body_url': '正文章节链接', 'reason': '适配理由',
          'note': '说明', 'ref': '引用', 'path': '本地路径', 'unconfirmed': '未确认项',
          'system_id': '系统ID（追溯）'}
SHEET_COLUMNS = ['范围', '序号', 'kind', 'type', 'title', 'case_no', 'content_version', 'url',
                 'section', 'index_url', 'body_url', 'reason', 'note', 'ref', 'path', 'unconfirmed',
                 'system_id', '其他字段']
# Every displayed key has a width; callers look widths up by key, not by position.
COLUMN_WIDTHS = {'范围': 14, '序号': 6, 'kind': 10, 'type': 12, 'title': 24, 'case_no': 18,
                 'content_version': 12, 'url': 40, 'section': 18, 'index_url': 40, 'body_url': 40,
                 'reason': 40, 'note': 24, 'ref': 24, 'path': 30, 'unconfirmed': 30, 'system_id': 24,
                 '其他字段': 24}
# Readable link text in Word; the URL itself is the hyperlink target.
LINK_TEXT = {'index_url': '索引记录', 'body_url': '正文章节'}
MISSING = '未提供'


def _is_url(value):
    return isinstance(value, str) and re.match(r'https?://[^\s]+$', value) is not None


def normalize_entry(value, where):
    """Return a dict copy of one source entry; unknown keys are preserved."""
    if isinstance(value, str):
        return {'ref': value}
    if not isinstance(value, dict):
        raise ValueError(where + ': 参考来源 entries must be strings or objects')
    entry = dict(value)
    for key in URL_KEYS:
        if entry.get(key) not in (None, '') and not _is_url(entry[key]):
            raise ValueError(where + ': ' + key + ' must be an explicit http(s) link; keep other references in ref')
    for key in SCALAR_KEYS:
        if key in entry and not (entry[key] is None or isinstance(entry[key], (str, int, float)) and not isinstance(entry[key], bool)):
            raise ValueError(where + ': ' + key + ' must be text')
    unconfirmed = entry.get('unconfirmed')
    if unconfirmed is not None and not isinstance(unconfirmed, str) and not (
            isinstance(unconfirmed, list) and all(isinstance(item, str) for item in unconfirmed)):
        raise ValueError(where + ': unconfirmed must be text or a list of text')
    return entry


def normalize(value, where='参考来源'):
    if value is None or value == '' or value == []:
        return []
    items = value if isinstance(value, list) else [value]
    return [normalize_entry(item, f'{where}[{index}]') for index, item in enumerate(items)]


def _present(entry, key):
    return entry.get(key) not in (None, '', [])


def missing_fields(entry):
    return [key for key in EXPECTED.get(entry.get('kind'), ()) if not _present(entry, key)]


def _text(value):
    if isinstance(value, list):
        return '；'.join(str(item) for item in value)
    return str(value)


def extra_fields(entry):
    return {key: entry[key] for key in entry if key not in KNOWN_KEYS}


def collect(cases_doc):
    """Document and case sources in display order with their scope."""
    document = cases_doc.get('document', {}) or {}
    rows = [('文档', index + 1, entry) for index, entry in
            enumerate(normalize(document.get('sources'), 'document.sources'))]
    for case in cases_doc.get('cases', []):
        if '参考来源' in case:
            entries = normalize(case['参考来源'], str(case.get('用例编号', '?')) + '.参考来源')
            rows += [(str(case.get('用例编号', '')), index + 1, entry) for index, entry in enumerate(entries)]
    return rows


def navigation(cases_doc):
    nav = (cases_doc.get('document', {}) or {}).get('navigation') or {}
    if not isinstance(nav, dict):
        raise ValueError('document.navigation must be an object')
    folder = nav.get('draft_folder_url')
    if folder not in (None, '') and not _is_url(folder):
        raise ValueError('navigation.draft_folder_url must be an explicit http(s) link')
    links = nav.get('links', [])
    if not isinstance(links, list) or any(not isinstance(link, dict) or not _is_url(link.get('url')) for link in links):
        raise ValueError('navigation.links must be objects with explicit http(s) url')
    editable = nav.get('editable_drafts', [])
    if not isinstance(editable, list) or any(not _is_url(url) for url in editable):
        raise ValueError('navigation.editable_drafts must be explicit http(s) links')
    return {'draft_folder_url': folder or None, 'links': links, 'editable_drafts': editable}


def summary(cases_doc):
    rows = collect(cases_doc)
    nav = navigation(cases_doc)
    return {'entries': len(rows),
            'missing': [{'scope': scope, 'index': index, 'kind': entry.get('kind'), 'fields': missing_fields(entry)}
                        for scope, index, entry in rows if missing_fields(entry)],
            'draft_folder_url': nav['draft_folder_url'] or 'not_provided'}


def has_content(cases_doc):
    nav = navigation(cases_doc)
    return bool(collect(cases_doc) or nav['draft_folder_url'] or nav['links'])


def cell_text(value):
    """Readable one-line-per-entry text for a mapped 参考来源 column."""
    lines = []
    for entry in normalize(value):
        parts = []
        for key in ('type', 'kind', 'title', 'case_no', 'content_version', 'url', 'section', 'index_url',
                    'body_url', 'reason', 'note', 'ref', 'path', 'unconfirmed', 'system_id'):
            if _present(entry, key):
                value_text = KIND_LABELS.get(entry[key], entry[key]) if key == 'kind' else _text(entry[key])
                parts.append(LABELS[key] + '：' + value_text)
        for key in missing_fields(entry):
            parts.append(LABELS[key] + '：' + MISSING)
        parts += [f'{key}={_text(val)}' for key, val in extra_fields(entry).items()]
        lines.append('｜'.join(parts) if parts else '（空来源）')
    return '\n'.join(lines)


def sheet_rows(cases_doc):
    """Rows for an XLSX/DOCX source table, aligned to SHEET_COLUMNS; links remain visible text."""
    nav = navigation(cases_doc)
    rows = []

    def nav_row(index, title, url):
        values = {'范围': '导航', '序号': index, 'kind': '导航', 'title': title, 'url': url}
        return [values.get(key, '') for key in SHEET_COLUMNS]
    if nav['draft_folder_url']:
        rows.append(nav_row(1, '草稿目录', nav['draft_folder_url']))
    for index, link in enumerate(nav['links']):
        rows.append(nav_row(index + 2, str(link.get('title', '链接')), link['url']))
    for scope, index, entry in collect(cases_doc):
        missing = missing_fields(entry)
        row = [scope, index]
        for key in SHEET_COLUMNS[2:-1]:
            if _present(entry, key):
                row.append(KIND_LABELS.get(entry[key], entry[key]) if key == 'kind' else _text(entry[key]))
            else:
                row.append(MISSING if key in missing else '')
        row.append('；'.join(f'{k}={_text(v)}' for k, v in extra_fields(entry).items()))
        rows.append(row)
    return rows


def link_text(key, row):
    """Readable text for a link cell of a sheet_rows row (title, else case number, else the URL)."""
    if key in LINK_TEXT:
        return LINK_TEXT[key]
    values = dict(zip(SHEET_COLUMNS, row))
    if values['范围'] == '导航' and values['title'] == '草稿目录':
        return '打开草稿目录'
    readable = [values[name] for name in ('title', 'case_no') if values[name] not in ('', MISSING)]
    return readable[0] if readable else values[key]


def sheet_header():
    return [LABELS.get(key, key) for key in SHEET_COLUMNS]


def _link(label, url):
    safe = url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')
    return '[' + label.replace('[', '\\[').replace(']', '\\]') + '](' + safe + ')'


def _md(value):
    return _text(value).replace('\n', ' ')


def _entry_block(heading, entry):
    lines = [heading]
    title = entry.get('title') or entry.get('case_no')
    if _present(entry, 'url'):
        lines.append('  - ' + LABELS['url'] + '：' + _link(_md(title or entry['url']), entry['url']))
    for key in ('type', 'kind', 'title', 'case_no', 'content_version', 'section'):
        if _present(entry, key):
            lines.append('  - ' + LABELS[key] + '：' + _md(KIND_LABELS.get(entry[key], entry[key]) if key == 'kind' else entry[key]))
    for key, label in (('index_url', '索引记录'), ('body_url', '正文章节')):
        if _present(entry, key):
            lines.append('  - ' + LABELS[key] + '：' + _link(label, entry[key]))
    for key in ('reason', 'note', 'ref', 'path', 'unconfirmed', 'system_id'):
        if _present(entry, key):
            lines.append('  - ' + LABELS[key] + '：' + _md(entry[key]))
    for key in missing_fields(entry):
        lines.append('  - ' + LABELS[key] + '：' + MISSING)
    extra = extra_fields(entry)
    if extra:
        lines.append('  - 其他字段：' + '；'.join(f'{k}={_md(v)}' for k, v in extra.items()))
    return '\n'.join(lines)


def markdown_section(cases_doc):
    """Blocks separated by blank lines so drafts split only between entries."""
    nav = navigation(cases_doc)
    blocks = ['## 来源与参考',
              '以下来源由编写 Agent 记录并复核；标“未提供”的字段没有可靠来源，脚本不推测 ID、版本或链接。']
    nav_lines = ['### 导航']
    nav_lines.append('- 草稿目录：' + (_link('打开草稿目录', nav['draft_folder_url']) if nav['draft_folder_url']
                                    else '未提供草稿目录链接（不推测 URL）'))
    nav_lines += ['- ' + _link(_md(link.get('title', link['url'])), link['url']) for link in nav['links']]
    blocks.append('\n'.join(nav_lines))
    rows = collect(cases_doc)
    document_rows = [row for row in rows if row[0] == '文档']
    case_rows = [row for row in rows if row[0] != '文档']
    if document_rows:
        blocks.append('### 文档来源')
        blocks += [_entry_block('- 来源 ' + str(index), entry) for _, index, entry in document_rows]
    if case_rows:
        blocks.append('### 用例参考')
        blocks += [_entry_block('- 用例 ' + _md(scope) + ' · 参考 ' + str(index), entry) for scope, index, entry in case_rows]
    if not rows:
        blocks.append('未记录需求或参考来源。')
    for block in blocks:
        lark_io.split_markdown(block + '\n')
    return '\n\n'.join(blocks) + '\n'


def resource_ids(value):
    """Identifiers in a link or token: path segments, query values, fragment."""
    if not isinstance(value, str) or not value:
        return set()
    parts = urlsplit(value)
    if not parts.scheme:
        return {value.strip()}
    ids = {segment for segment in parts.path.split('/') if len(segment) >= 8}
    ids.update(v for _, v in parse_qsl(parts.query) if len(v) >= 8)
    return ids


def formal_ids(cases_doc=None, library=None):
    """Sources and formal library resources that Write must never modify."""
    ids = set()
    if cases_doc is not None:
        for _, _, entry in collect(cases_doc):
            for key in URL_KEYS + ('ref',):
                ids |= resource_ids(entry.get(key))
    if library:
        ids |= resource_ids(library.get('base_token'))
        for mapping_key in ('tables', 'table_base_tokens'):
            mapping = library.get(mapping_key) or {}
            if isinstance(mapping, dict):
                for token in mapping.values():
                    ids |= resource_ids(token)
        folders = library.get('folders') or {}
        for key in ('bodies', 'originals'):
            ids |= resource_ids(folders.get(key))
    return ids
