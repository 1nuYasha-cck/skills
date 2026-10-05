"""Self-contained, user-identity lark-cli transport. No semantic decisions."""
import json
import hashlib
from html.parser import HTMLParser
import re
import subprocess
import time
from pathlib import Path


def redact(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'(?i)secret|authorization|access_token|refresh_token|password', k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[redacted]', value)
    return value


class LarkError(Exception):
    def __init__(self, code, message, command=None, detail=None):
        self.code, self.message = str(code), redact(str(message))
        self.command, self.detail = redact(command or []), redact(detail)
        super().__init__(self.message)

    def as_dict(self):
        return dict(code=self.code, message=self.message, command=self.command, detail=self.detail)


def run_lark(args, *, dry_run=False, timeout=120, runner=subprocess.run, json_flag=True, input_text=None, cwd=None):
    if any(a == '--as' or a.startswith('--as=') for a in args):
        raise ValueError('identity is fixed to user; do not pass --as')
    command = ['lark-cli', *map(str, args), '--as', 'user']
    if json_flag and '--format' not in args:
        command += ['--format', 'json']
    if dry_run:
        return {'dry_run': True, 'command': redact(command + ['--dry-run']), **({'cwd': str(cwd)} if cwd is not None else {})}
    # A write with an unknown outcome must be read back, never blindly retried.
    read_only = any(a in {'+record-list', '+record-search', '+table-list', '+field-list', '+fetch', '+search'} for a in args) or args[:3] == ['drive', 'files', 'list']
    for attempt in range(3):
        try:
            kwargs = dict(capture_output=True, text=True, timeout=timeout)
            if cwd is not None:
                kwargs["cwd"] = str(cwd)
            if input_text is not None:
                kwargs["input"] = input_text
            result = runner(command, **kwargs)
        except (subprocess.TimeoutExpired, OSError) as exc:
            error = LarkError('timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'transport', str(exc), command)
        else:
            parsed = None
            for raw in ([result.stderr, result.stdout] if result.returncode else [result.stdout]):
                try:
                    parsed = json.loads(raw)
                    break
                except (ValueError, TypeError):
                    continue
            if result.returncode == 0 and isinstance(parsed, dict) and parsed.get('ok', True) is not False and parsed.get('code', 0) in (0, '0', None):
                return parsed
            detail = parsed if isinstance(parsed, dict) else {'stderr': result.stderr, 'stdout': result.stdout}
            e = detail.get('error', detail)
            if not isinstance(e, dict):
                e = {'message': str(e)}
            error = LarkError(e.get('code', e.get('type', f'exit_{result.returncode}')), e.get('message', 'invalid response or CLI failure'), command, detail)
        transient = bool(re.search(r'429|5\d\d|timeout|rate.limit|temporar', error.code + ' ' + error.message, re.I))
        if not (read_only and transient and attempt < 2):
            raise error
        time.sleep(0.25 * 2 ** attempt)


def data(response):
    return response.get('data', response)


def _records_page(payload):
    """Normalize the verified raw matrix, with strict shape validation."""
    if 'record_id_list' in payload:
        ids, names, values = payload['record_id_list'], payload['fields'], payload['data']
        if len(ids) != len(values) or any(len(row) != len(names) for row in values):
            raise LarkError('response_shape', 'record matrix dimensions differ')
        return [{'record_id': rid, 'fields': dict(zip(names, row))} for rid, row in zip(ids, values)]
    rows = payload.get('records', payload.get('items'))
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get('record_id') or not isinstance(r.get('fields'), dict) for r in rows):
        raise LarkError('response_shape', 'missing record matrix or records')
    return rows


def _read_records(base, table, extra, page_size):
    if not 1 <= page_size <= 200:
        raise ValueError('page size must be 1..200')
    records, pages, offset = [], 0, 0
    seen = set()
    try:
        while True:
            payload = data(run_lark(['base', extra[0], '--base-token', base, '--table-id', table, '--limit', str(page_size), '--offset', str(offset), *extra[1:]]))
            rows = _records_page(payload)
            page_ids = {r['record_id'] for r in rows}
            if len(page_ids) != len(rows) or seen.intersection(page_ids):
                raise LarkError('pagination', 'duplicate record within or across pages')
            seen.update(page_ids)
            records.extend(rows)
            pages += 1
            if type(payload.get('has_more')) is not bool:
                raise LarkError('response_shape', 'has_more missing or not boolean')
            if not payload['has_more']:
                return dict(records=records, complete=True, pages=pages, error=None)
            next_offset = payload.get('offset', offset + len(rows))
            if not rows or not isinstance(next_offset, int) or next_offset <= offset:
                raise LarkError('pagination', 'pagination made no progress')
            offset = next_offset
    except LarkError as exc:
        return dict(records=records, complete=False, pages=pages, error=exc.as_dict())


def list_records(base, table, *, fields=None, view=None, page_size=200):
    extra = ['+record-list']
    for name in fields or []:
        extra += ['--field-id', name]
    if view:
        extra += ['--view-id', view]
    return _read_records(base, table, extra, page_size)


def search_records(base, table, keyword, search_fields, *, select_fields=None, limit=200):
    if not keyword or not 1 <= len(search_fields) <= 20:
        raise ValueError('keyword and 1..20 search fields required')
    extra = ['+record-search', '--keyword', keyword]
    for name in search_fields:
        extra += ['--search-field', name]
    for name in select_fields or []:
        extra += ['--field-id', name]
    result = _read_records(base, table, extra, limit)
    return dict(result, keyword=keyword, search_fields=search_fields)


def list_tables(base):
    return data(run_lark(['base', '+table-list', '--base-token', base]))


def list_fields(base, table):
    return data(run_lark(['base', '+field-list', '--base-token', base, '--table-id', table]))


def create_base(name, folder_token=None, *, dry_run=False):
    args = ['base', '+base-create', '--name', name]
    if folder_token:
        args += ['--folder-token', folder_token]
    return data(run_lark(args, dry_run=dry_run))


def create_table(base, name, fields, *, dry_run=False):
    return data(run_lark(['base', '+table-create', '--base-token', base, '--name', name, '--fields', json.dumps(fields, ensure_ascii=False)], dry_run=dry_run))


def _batch(base, table, rows, update=False, dry_run=False):
    results, completed, record_ids = [], [], []
    for start in range(0, len(rows), 200):
        chunk = rows[start:start + 200]
        body = {'update_records': {r['record_id']: r['fields'] for r in chunk}} if update else {'create_records': chunk}
        try:
            response = data(run_lark(['base', '+record-batch-update' if update else '+record-batch-create', '--base-token', base, '--table-id', table, '--json', json.dumps(body, ensure_ascii=False)], dry_run=dry_run))
            results.append(response)
            record_ids.extend(response.get("record_id_list", [r.get("record_id") for r in response.get("records", []) if r.get("record_id")]))
            completed.extend(chunk)
        except LarkError as exc:
            return dict(status='partial' if completed else 'failed', completed=completed, results=results, record_ids=record_ids, failed_slice=chunk, error=exc.as_dict(), unknown_outcome=True)
    return dict(status='ok', completed=completed, results=results, record_ids=record_ids)


def batch_create(base, table, rows, *, dry_run=False):
    return _batch(base, table, rows, dry_run=dry_run)


def batch_update(base, table, updates, *, dry_run=False):
    return _batch(base, table, updates, True, dry_run)


def create_folder(name, parent_token=None, *, dry_run=False):
    args = ['drive', '+create-folder', '--name', name]
    if parent_token:
        args += ['--folder-token', parent_token]
    return data(run_lark(args, dry_run=dry_run))


def upload_file(path, folder_token, *, dry_run=False):
    source = Path(path).expanduser().resolve(strict=True)
    return data(run_lark(['drive', '+upload', '--file', source.name, '--folder-token', folder_token], dry_run=dry_run, cwd=source.parent))


def list_folder(folder_token):
    files, cursor, seen = [], None, set()
    while True:
        args = ['drive', 'files', 'list', '--folder-token', folder_token, '--page-size', '200']
        if cursor:
            args += ['--page-token', cursor]
        response = data(run_lark(args))
        rows = response.get('files')
        if not isinstance(rows, list) or type(response.get('has_more')) is not bool:
            raise LarkError('response_shape', 'folder listing must have files and has_more')
        files.extend(rows)
        if not response['has_more']:
            return files
        cursor = response.get('next_page_token', response.get('page_token'))
        if not cursor or cursor in seen:
            raise LarkError('pagination', 'folder listing cursor missing or repeated')
        seen.add(cursor)


def _doc_from_files(files, title):
    candidates = [f for f in files if f.get('name') == title and f.get('type') == 'docx' and f.get('url')]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f.get('modified_time', ''), f.get('created_time', ''), f.get('token', '')), reverse=True)
    return dict(document=candidates[0], recovered=True, resources=candidates, superseded=candidates[1:])


def _unknown_write(error):
    return bool(re.search(r'network|timeout|time.?out|transport|exit_|response_shape|5\d\d', error.code + ' ' + error.message, re.I))


def split_markdown(markdown, max_bytes=6000, max_lines=80):
    """Split only between Markdown blocks, preserving complete tables."""
    chunks, current = [], ''
    for block in markdown.split('\n\n'):
        if not block:
            continue
        if len(block.encode('utf-8')) > max_bytes or block.count('\n') + 1 > max_lines:
            raise ValueError('single Markdown block exceeds safe upload size; split this field/table explicitly')
        candidate = current + ('\n\n' if current else '') + block
        if current and (len(candidate.encode('utf-8')) > max_bytes or candidate.count('\n') + 1 > max_lines):
            chunks.append(current + '\n'); current = block
        else:
            current = candidate
    if current:
        chunks.append(current + '\n')
    return chunks or ['\n']


class _XMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.text = []
    def handle_data(self, value):
        self.text.append(value)


def _plain_markdown(markdown):
    parts = []
    for line in markdown.splitlines():
        if re.fullmatch(r'\s*\|?[ \t:|\-]+\|?\s*', line):
            continue
        line = re.sub(r'^#{1,6}\s+', '', line)
        # Table boundaries only; escaped literal pipes remain literal text.
        line = re.sub(r'(?<!\\)\|', '', line)
        line = re.sub(r'\\([\\`*_\[\]$~<>|#+-])', r'\1', line)
        line = line.replace('<br>', '\n').replace('<br/>', '\n')
        parts.append(line)
    return re.sub(r'\s+', '', ''.join(parts))


def _committed(doc, content):
    fetched = fetch_doc(doc, doc_format='xml')
    parser = _XMLText(); parser.feed(fetched['content'])
    actual = re.sub(r'\s+', '', ''.join(parser.text))
    return _plain_markdown(content) in actual


def update_doc(doc, markdown, *, dry_run=False):
    chunks = split_markdown(markdown)
    results = []
    for index, chunk in enumerate(chunks):
        args = ['docs', '+update', '--doc', doc, '--command', 'overwrite' if index == 0 else 'append', '--doc-format', 'markdown', '--content', '-']
        try:
            response = data(run_lark(args, dry_run=dry_run, json_flag=False, input_text=chunk))
        except LarkError as exc:
            if not _unknown_write(exc):
                raise
            # An append is never retried. Verify the cumulative prefix before proceeding.
            expected = '\n\n'.join(c.rstrip() for c in chunks[:index + 1])
            verified = False
            try:
                for probe in range(3):
                    if _committed(doc, expected):
                        verified = True; break
                    if probe < 2:
                        time.sleep(0.5 * 2 ** probe)
            except LarkError as read_error:
                exc.detail = dict(original=exc.as_dict(), readback_error=read_error.as_dict())
            if not verified:
                raise LarkError('document_unknown', 'document update unconfirmed; read back before another write', detail=dict(doc=doc, chunk=index, error=exc.as_dict(), unknown_outcome=True)) from exc
            response = dict(recovered=True, chunk=index)
        results.append(response)
    return dict(document=dict(url=doc), chunks=len(chunks), results=results, content_sha256=hashlib.sha256(markdown.encode('utf-8')).hexdigest())


def create_doc(title, markdown, parent_token=None, *, dry_run=False, allow_create=True):
    # Validate all blocks before creating a remote resource.
    split_markdown(markdown)
    args = ['docs', '+create', '--title', title, '--doc-format', 'markdown', '--content', '-']
    if parent_token:
        args += ['--parent-token', parent_token]
    if dry_run:
        return dict(data(run_lark(args, dry_run=True, json_flag=False, input_text=markdown)), markdown=markdown)
    existing = _doc_from_files(list_folder(parent_token), title) if parent_token else None
    if existing:
        result = existing
    else:
        if not allow_create:
            raise LarkError('document_unknown', 'previous creation remains unconfirmed; refusing to create again', detail=dict(title=title, parent_token=parent_token, unknown_outcome=True))
        try:
            # Lightweight creation yields a durable identity before uploading the body.
            result = data(run_lark(args, json_flag=False, input_text='# ' + title + '\n'))
        except LarkError as exc:
            if not parent_token or not _unknown_write(exc):
                raise
            result = None
            try:
                for probe in range(3):
                    result = _doc_from_files(list_folder(parent_token), title)
                    if result:
                        break
                    if probe < 2:
                        time.sleep(0.5 * 2 ** probe)
            except LarkError as probe_error:
                raise LarkError('document_unknown', 'creation and folder readback unconfirmed', detail=dict(title=title, parent_token=parent_token, error=exc.as_dict(), readback_error=probe_error.as_dict(), unknown_outcome=True)) from exc
            if not result:
                raise LarkError('document_unknown', 'creation unconfirmed; no second create attempted', detail=dict(title=title, parent_token=parent_token, error=exc.as_dict(), unknown_outcome=True)) from exc
    document = result.get('document', result)
    url = document.get('url', document.get('document_url'))
    if not url:
        raise LarkError('response_shape', 'created document URL missing', detail=result)
    try:
        result['body_upload'] = update_doc(url, markdown)
    except LarkError as exc:
        raise LarkError(exc.code, exc.message, exc.command, dict(error=exc.as_dict(), resources=[document], title=title, unknown_outcome=True)) from exc
    return result


def fetch_doc(doc, *, doc_format='markdown'):
    result = data(run_lark(['docs', '+fetch', '--doc', doc, '--doc-format', doc_format], json_flag=False))
    document = result.get('document', result)
    return dict(result, content=document.get('content', document.get('markdown', document.get('text', ''))), revision=document.get('revision_id', document.get('revision')))


def search_docs(query, page_size=20, max_pages=5, filter=None):
    items, token = [], None
    try:
        for page in range(max_pages):
            args = ['docs', '+search', '--query', query, '--page-size', str(page_size)]
            if token:
                args += ['--page-token', token]
            if filter:
                args += ['--filter', json.dumps(filter, ensure_ascii=False)]
            response = data(run_lark(args))
            items.extend(response.get('results', response.get('items', response.get('docs', []))))
            token = response.get('page_token', response.get('next_page_token'))
            if not response.get('has_more', bool(token)):
                return dict(items=items, complete=True, pages=page + 1, error=None)
            if not token:
                raise LarkError('pagination', 'missing document search cursor')
        return dict(items=items, complete=False, pages=max_pages, error=None, page_token=token)
    except LarkError as exc:
        return dict(items=items, complete=False, error=exc.as_dict())


def safe_output(path, inputs=(), *, sources=None, overwrite=False, directory=False):
    """Validate outputs before any remote side effect; resolve symlinks."""
    target = Path(path).expanduser().resolve()
    for item in inputs:
        source = Path(item).expanduser().resolve()
        if target == source or (directory and source.is_relative_to(target)):
            raise ValueError('output must not replace or contain an input file')
    # Legacy callers treat positional inputs as source documents. Explicit sources
    # lets callers distinguish configuration/plan inputs from protected originals.
    for item in inputs if sources is None else sources:
        source = Path(item).expanduser().resolve()
        if target.is_relative_to(source.parent):
            raise ValueError('output must be outside source file directories')
    if target.exists() and not overwrite:
        raise ValueError('output exists; use --overwrite')
    if target.exists() and (target.is_dir() != directory):
        raise ValueError('output has incompatible type')
    return target
