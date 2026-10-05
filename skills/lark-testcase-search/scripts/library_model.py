"""Optional config 2.0 extensions: schema profiles, field maps, per-table bases,
publication policy and body locators. Mechanical only; no relevance judgment."""
import json
import re
from html import unescape
from urllib.parse import parse_qsl, urlsplit

import lark_io as io

PROFILES = ('standard', 'indexed')
STANDARD_FIELDS = ['用例编号', '用例标题', '摘要', '关键词', '需求编号', '测试步骤', '预期结果', '所属分类']
# Index summary fields; full steps and criteria live in the linked body section.
INDEXED_FIELDS = ['原用例编号', '标题', '摘要', '关键词', '需求编号', '功能点', '模块', '项目', '检索文本']
# Logical role -> canonical field name; field_maps.cases may rename canonical names.
ROLES = {
    'standard': {'display_id': '用例编号', 'title': '用例标题'},
    'indexed': {'case_id': '系统用例ID', 'display_id': '原用例编号', 'title': '标题', 'content_version': '内容版本',
                'doc_version': '文档版本', 'body_link': '正文链接', 'doc_token': '文档token', 'block_id': '章节block ID',
                'publication': '发布状态', 'review': '审核状态'},
}
SCOPES = ('published', 'all', 'unpublished')
VERSION_LABEL = '内容版本'


def _strings(value):
    return isinstance(value, str) and bool(value.strip())


def validate_library(lib):
    """Validate only the optional keys this Skill supports; absent keys keep old behavior."""
    profile = lib.get('schema_profile', 'standard')
    if profile not in PROFILES:
        raise ValueError('schema_profile must be standard or indexed')
    tables = lib['tables']
    if any(not _strings(v) for v in tables.values()):
        raise ValueError('tables values must be nonempty table_id strings')
    bases = lib.get('table_base_tokens', {})
    if not isinstance(bases, dict) or any(k not in tables or not _strings(v) for k, v in bases.items()):
        raise ValueError('table_base_tokens keys must be configured tables with nonempty base tokens')
    maps = lib.get('field_maps', {})
    if not isinstance(maps, dict):
        raise ValueError('field_maps must be an object')
    for key, mapping in maps.items():
        if key not in tables or not isinstance(mapping, dict) or not mapping:
            raise ValueError('field_maps keys must be configured tables with nonempty objects')
        if any(not _strings(k) or not _strings(v) for k, v in mapping.items()):
            raise ValueError('field_maps entries must map nonempty names')
        if len(set(mapping.values())) != len(mapping):
            raise ValueError('field_maps maps two canonical names to one field')
    policy = lib.get('search_policy')
    if policy is not None:
        if not isinstance(policy, dict) or policy.get('default_scope') not in ('published', 'all'):
            raise ValueError('search_policy.default_scope must be published or all')
        if not _strings(policy.get('publication_field')):
            raise ValueError('search_policy.publication_field required')
        values = policy.get('published_values')
        if not isinstance(values, list) or not values or not all(_strings(v) for v in values):
            raise ValueError('search_policy.published_values must be nonempty strings')
    return lib


def profile(lib):
    return lib.get('schema_profile', 'standard')


def table_base(lib, key):
    return lib.get('table_base_tokens', {}).get(key, lib['base_token'])


def real_field(lib, logical, table='cases'):
    return lib.get('field_maps', {}).get(table, {}).get(logical, logical)


def default_search_fields(lib):
    """Return (fields, rule) or None when the Agent must read the schema."""
    if lib.get('schema_profile') == 'indexed':
        return [real_field(lib, f) for f in INDEXED_FIELDS], 'indexed_profile_summary_fields'
    if lib['kind'] == 'standard':
        return [real_field(lib, f) for f in STANDARD_FIELDS], 'standard_eight_text_fields'
    return None


def applied_maps(lib, names, table='cases'):
    mapping = lib.get('field_maps', {}).get(table, {})
    return {k: v for k, v in mapping.items() if v in names}


def text_values(value):
    """Normalize text/select/link cell shapes into strings; unknown shapes yield None."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [str(value)] if str(value).strip() else []
    if isinstance(value, dict):
        for key in ('text', 'name', 'link', 'value'):
            if isinstance(value.get(key), str):
                return [value[key]] if value[key].strip() else []
        return None
    if isinstance(value, list):
        out = []
        for item in value:
            part = text_values(item)
            if part is None:
                return None
            out.extend(part)
        return out
    return None


def single_text(value):
    values = text_values(value)
    return ''.join(values).strip() if values else None


def role_values(lib, fields):
    """Logical role values with the real field name each came from."""
    roles = ROLES.get(profile(lib), {})
    out = {}
    for role, canonical in roles.items():
        name = real_field(lib, canonical)
        if name in fields:
            out[role] = {'field': name, 'canonical': canonical, 'value': single_text(fields[name])}
    return out


URL_RE = re.compile(r'https?://[^\s)\]<>"]+')


def parse_body(roles, record_url=None):
    link = (roles.get('body_link') or {}).get('value')
    url = None
    link_token = link_anchor = None
    if link:
        match = URL_RE.search(link)
        if match:
            url = match.group(0)
            found = re.search(r'/(?:docx|wiki)/([A-Za-z0-9]+)(?:[?][^#]*)?(?:#([A-Za-z0-9_-]+))?', url)
            if found:
                link_token, link_anchor = found.group(1), found.group(2)
    token = (roles.get('doc_token') or {}).get('value')
    block = (roles.get('block_id') or {}).get('value')
    conflicts = []
    if token and link_token and token != link_token:
        conflicts.append('doc_token differs from body link')
    if block and link_anchor and block != link_anchor:
        conflicts.append('block_id differs from body link anchor')
    if not (url or token or block):
        return None
    body = {'url': url, 'doc_token': token or link_token, 'block_id': block or link_anchor,
            'link_anchor': link_anchor, 'conflicts': conflicts}
    if body['doc_token'] and body['block_id'] and not conflicts:
        doc = url.split('#', 1)[0] if url else body['doc_token']
        command = ['doc-read', '--doc', doc, '--block-id', body['block_id']]
        # Identity is proven by the section's unique link back to its index record when a
        # record URL is known; the system ID check remains for sections without one.
        # The system ID stays as a fallback for legacy `<ID> / 内容版本 N` headings only.
        if record_url:
            command += ['--expect-record-url', record_url]
        for role, flag in (('case_id', '--expect-case-id'), ('content_version', '--expect-content-version'),
                           ('doc_version', '--expect-doc-revision')):
            value = (roles.get(role) or {}).get('value')
            if value:
                command += [flag, value]
        body['read_args'] = command
    return body


def describe(lib, record, record_url=None):
    """Readable labels plus technical traceability; raw fields stay untouched."""
    roles = role_values(lib, record['fields'])
    display_id = (roles.get('display_id') or {}).get('value')
    title = (roles.get('title') or {}).get('value')
    out = {'display': {'display_id': display_id, 'title': title,
                       'label': ' '.join(v for v in (display_id, title) if v) or None},
           'logical_fields': roles}
    identity = {'record_id': record['record_id']}
    for role in ('case_id', 'content_version', 'doc_version'):
        if role in roles:
            identity[role] = roles[role]['value']
    out['identity'] = identity
    body = parse_body(roles, record_url)
    if body:
        out['body'] = body
    return out


# Publication scope -----------------------------------------------------------

def scope_spec(lib, requested):
    policy = lib.get('search_policy')
    if requested is not None and requested not in SCOPES:
        raise ValueError('scope must be published, all or unpublished')
    if requested is None:
        if not policy:
            return {'scope': 'all', 'source': 'no_policy', 'mode': 'none'}
        scope, source = policy['default_scope'], 'policy_default'
    else:
        scope, source = requested, 'requested'
    if scope != 'all' and not policy:
        raise ValueError('scope ' + scope + ' requires search_policy for library ' + lib['name'])
    spec = {'scope': scope, 'source': source}
    if policy:
        spec.update(field=real_field(lib, policy['publication_field']), published_values=list(policy['published_values']))
    # Verified CLI filter: select intersects [values]. Text intersects is a contains match,
    # so every returned row is still checked locally for exact values.
    spec['mode'] = {'published': 'server_intersects_then_local_exact', 'unpublished': 'local_exclude_published',
                    'all': 'none'}[scope]
    return spec


def server_filter(spec):
    if spec['mode'] != 'server_intersects_then_local_exact':
        return None
    return json.dumps({'logic': 'and', 'conditions': [[spec['field'], 'intersects', spec['published_values']]]}, ensure_ascii=False)


def publication_state(spec, fields):
    if spec.get('field') is None:
        return None
    if spec['field'] not in fields:
        return 'unknown'
    values = text_values(fields[spec['field']])
    if not values:
        return 'unknown'
    return 'published' if all(v in spec['published_values'] for v in values) else 'unpublished'


def apply_scope(spec, records):
    """Keep in-scope records; report every exclusion and the observed status values."""
    report = dict(spec, read=len(records), kept=0, excluded={'published': 0, 'unpublished': 0, 'unknown': 0}, status_counts={})
    if spec['mode'] == 'server_intersects_then_local_exact':
        report['note'] = 'records excluded by the server filter were not downloaded; their count is unknown'
    if spec['mode'] == 'none' and spec['source'] == 'no_policy':
        report['note'] = 'no search_policy; publication status not filtered'
    kept = []
    for record in records:
        state = publication_state(spec, record['fields'])
        if state is not None:
            raw = record['fields'].get(spec['field'])
            label = '<missing>' if spec['field'] not in record['fields'] else ('<empty>' if raw in (None, '', []) else json.dumps(raw, ensure_ascii=False))
            report['status_counts'][label] = report['status_counts'].get(label, 0) + 1
        keep = (spec['scope'] == 'all' or (spec['scope'] == 'published' and state == 'published')
                or (spec['scope'] == 'unpublished' and state != 'published'))
        if keep:
            kept.append(dict(record, publication_state=state) if state else record)
        else:
            report['excluded'][state] += 1
    report['kept'] = len(kept)
    return kept, report


def read_filtered(lib, command, keyword=None, search_fields=(), spec=None, page_size=200):
    """Paginate with the verified --filter-json flag through the shared reader."""
    base, table = table_base(lib, 'cases'), lib['tables']['cases']
    extra = [command]
    if command == '+record-search':
        extra += ['--keyword', keyword]
        for name in search_fields:
            extra += ['--search-field', name]
    extra += ['--filter-json', server_filter(spec)]
    return io._read_records(base, table, extra, page_size)


# Body section reading ---------------------------------------------------------

def split_doc(doc):
    if '#' not in doc:
        return doc, None
    head, fragment = doc.split('#', 1)
    return head, fragment or None


def fetch_section(doc, block_id):
    payload = io.data(io.run_lark(['docs', '+fetch', '--doc', doc, '--doc-format', 'markdown', '--scope', 'section',
                                   '--start-block-id', block_id], json_flag=False))
    document = payload.get('document', payload)
    content = document.get('content')
    if not isinstance(content, str):
        raise io.LarkError('response_shape', 'section fetch returned no content')
    opening = re.match(r'\A\s*<fragment\b([^>]*)>', content)
    if not opening or 'mode="section"' not in opening.group(1) or 'requested-start="' + block_id + '"' not in opening.group(1):
        raise io.LarkError('section_unconfirmed', 'response is not a section fragment for the requested block')
    inner = re.sub(r'</fragment>\s*\Z', '', content[opening.end():]).strip()
    if not inner:
        raise io.LarkError('section_empty', 'requested section returned no content')
    return {'content': content, 'revision': document.get('revision_id', document.get('revision')),
            'document_id': document.get('document_id'), 'excerpt_only': '<excerpt' in content}


ID_CHARS = r'A-Za-z0-9_\-'


def token_in(value, text):
    """Exact token match: TC1 never matches inside TC10 or TC1_2."""
    return bool(re.search(r'(?<![' + ID_CHARS + r'])' + re.escape(value) + r'(?![' + ID_CHARS + r'])', text))


def heading_version(heading):
    """Old `<ID> / 内容版本 N` and new `编号 标题（内容版本 N）` headings."""
    found = re.findall(re.escape(VERSION_LABEL) + r'\s*[:：]?\s*([0-9A-Za-z._-]+)', heading)
    return found[-1] if found else None


def record_key(url):
    """(base token, table, record) of an index record link; None when not one."""
    parts = urlsplit(unescape(url))
    query = dict(parse_qsl(parts.query))
    base = re.search(r'/base/([A-Za-z0-9_-]+)', parts.path)
    if not query.get('record') or not query.get('table') or not base:
        return None
    return base.group(1), query['table'], query['record']


def record_backlinks(content):
    text = re.sub(r'\\([\\_*\[\]()&#])', r'\1', content)
    keys = [record_key(u) for u in re.findall(r'https?://[^\s)\]<>"\']+', text)]
    return list(dict.fromkeys(k for k in keys if k))


def check_consistency(content, revision, expect_case_id=None, expect_version=None, expect_revision=None, expect_record_url=None):
    heading = next((line.lstrip('#').strip() for line in content.splitlines() if line.lstrip().startswith('#')), '')
    checks = {}
    if expect_record_url:
        expected = record_key(expect_record_url)
        if expected is None:
            raise ValueError('--expect-record-url must be an index record link with table and record')
        links = record_backlinks(content)
        # Exactly one index record link must exist; several make identity unprovable.
        if not links:
            checks['record_backlink'] = 'not_found'
        elif len(links) > 1:
            checks['record_backlink'] = 'ambiguous'
        else:
            checks['record_backlink'] = 'match' if links[0] == expected else 'mismatch'
    if expect_case_id:
        checks['case_id'] = 'match' if token_in(expect_case_id, heading) else (
            'found_outside_heading' if token_in(expect_case_id, content) else 'not_found')
    if expect_version:
        found = heading_version(heading)
        checks['content_version'] = 'not_found' if found is None else ('match' if found == str(expect_version).strip() else 'mismatch')
    if expect_revision:
        checks['doc_revision'] = 'not_found' if revision is None else ('match' if str(revision) == str(expect_revision).strip() else 'mismatch')
    # Identity: the unique record backlink is primary; a legacy heading token is the fallback.
    # The legacy heading ID may stand in only when the section has no record backlink at all;
    # an ambiguous or mismatching backlink can never be overridden by it.
    backlink = checks.get('record_backlink')
    if backlink == 'match':
        basis = 'record_backlink'
    elif backlink in (None, 'not_found') and checks.get('case_id') == 'match':
        basis = 'case_id'
    else:
        basis = None
    others = [v for k, v in checks.items() if k not in ('record_backlink', 'case_id')]
    identity_checked = any(k in checks for k in ('record_backlink', 'case_id'))
    if not checks:
        verdict = 'not_checked'
    elif 'mismatch' in checks.values():
        verdict = 'mismatch'
    elif (basis or not identity_checked) and all(v == 'match' for v in others):
        verdict = 'consistent'
    else:
        verdict = '证据不足'
    return {'heading': heading or None, 'checks': checks, 'identity_basis': basis, 'verdict': verdict}
