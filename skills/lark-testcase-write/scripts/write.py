#!/usr/bin/env python3
"""Agent workflow tools: extract, inspect, fill, coverage, defaults, drafts."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import lark_io
import source_trace
from doc_extract import extract_document, render_extract
from templates import (protect_output, inspect_template, fill_xlsx, fill_docx,
                       render_markdown, resolve_template, set_default_template, detect_format)
from coverage import compute_coverage, coverage_markdown


BACKLINK_PLACEHOLDER = '<创建后的草稿链接>'


def navigation_markdown(folder_url, files, sources_state):
    lines = ['## 草稿导航', '']
    lines.append('- 草稿目录：' + (source_trace._link('返回草稿目录', folder_url) if folder_url
                                    else '未提供草稿目录链接（不推测 URL）'))
    names = [Path(path).name for path in files]
    lines.append('- 同批上传附件：' + ('、'.join(names) + '（与本草稿位于同一目录）' if names else '无'))
    lines.append('- 需求与参考来源：' + {'appended': '见“来源与参考”一节', 'already_present': '见“来源与参考”一节',
                                    'none_recorded': 'cases.json 未记录来源'}.get(sources_state, '未提供 cases.json，未附加来源'))
    lines.append('- 性质：草稿，不是正式入库；需要入库时由 Maintain 按普通文档流程审核。')
    return '\n'.join(lines) + '\n'


def plan_backlinks(targets, markdown, cases_doc, library_config):
    """Only explicitly declared editable drafts; never sources or formal resources."""
    if not targets:
        return []
    if markdown is None:
        raise ValueError('Backlinks require a Markdown draft created in this run')
    editable = set(source_trace.navigation(cases_doc)['editable_drafts']) if cases_doc else set()
    forbidden = source_trace.formal_ids(cases_doc, library_config)
    plan = []
    for target in targets:
        if not source_trace._is_url(target):
            raise ValueError('Backlink target must be an explicit http(s) link: ' + str(target))
        if target not in editable:
            raise ValueError('Backlink target is not declared in document.navigation.editable_drafts: ' + target)
        if not re.search(r'/(docx|wiki)/', target):
            raise ValueError('Backlink targets must be docx/wiki draft documents: ' + target)
        if source_trace.resource_ids(target) & forbidden:
            raise ValueError('Backlink target is a requirement/reference source or formal library resource: ' + target)
        plan.append({'target': target, 'action': 'planned'})
    return plan


def backlink_markdown(title, url):
    return '## 关联测试用例草稿\n\n- ' + source_trace._link(title, url) + '（草稿，不是正式入库）\n'


def upload_draft(config_or_folder, files, markdown=None, title=None, *, dry_run=False,
                 library=None, folder_key=None, replace_draft=False, cases_doc=None,
                 draft_folder_url=None, backlinks=()):
    library_config = None
    if isinstance(config_or_folder, dict):
        if not library or not folder_key:
            raise ValueError('Config upload requires explicit library and folder_key')
        libraries = config_or_folder.get('libraries', [])
        matches = [item for item in libraries if item.get('name') == library]
        if len(matches) != 1:
            raise ValueError('Library name must identify exactly one configured library')
        # Formal base/table settings are never publish targets; they only feed the backlink denylist.
        library_config = matches[0]
        folders = matches[0].get('folders', {})
        folder = folders.get(folder_key)
        reserved = [folders.get(key) for key in ('bodies', 'originals')]
        if folder_key in ('bodies', 'originals') or (folder and folder in reserved):
            raise ValueError('Drafts cannot use the library bodies/originals folder')
    else:
        if library is not None or folder_key is not None:
            raise ValueError('library/folder_key only apply to config upload')
        folder = config_or_folder
    if not isinstance(folder, str) or not folder:
        raise ValueError('Specify one draft folder explicitly')
    nav = source_trace.navigation(cases_doc) if cases_doc is not None else {'draft_folder_url': None}
    folder_url = draft_folder_url or nav['draft_folder_url']
    if folder_url is not None and not source_trace._is_url(folder_url):
        raise ValueError('Draft folder URL must be an explicit http(s) link')
    result = {'status': 'ok', 'draft_only': True, 'files': [], 'document': None, 'dry_run': dry_run}
    for path in files:
        if not Path(path).is_file():
            raise ValueError('Missing upload file: ' + str(path))
    draft_title = None
    existing = []
    if markdown is not None:
        draft_title = title if title is not None else '测试用例草稿'
        if not isinstance(draft_title, str) or not draft_title.strip() or '\n' in draft_title or '\r' in draft_title:
            raise ValueError('Draft title must be a nonempty single line')
        draft_title = draft_title.strip()
        heading = re.compile(r'^ {0,3}#[ \t]+[^\r\n]*', re.MULTILINE)
        if heading.search(markdown):
            markdown = heading.sub(lambda match: '# ' + draft_title, markdown, count=1)
        else:
            markdown = '# ' + draft_title + '\n\n' + markdown
        if cases_doc is None:
            sources_state = 'no_cases'
        elif not source_trace.has_content(cases_doc):
            sources_state = 'none_recorded'
        elif '## 来源与参考' in markdown:
            sources_state = 'already_present'
        else:
            sources_state = 'appended'
            markdown = markdown.rstrip('\n') + '\n\n' + source_trace.markdown_section(cases_doc)
        markdown = markdown.rstrip('\n') + '\n\n' + navigation_markdown(folder_url, files, sources_state)
        result['navigation'] = {'draft_folder_url': folder_url,
                                'status': 'explicit' if folder_url else 'not_provided_not_guessed',
                                'sources_section': sources_state}
        # Preflight all local content before uploads. Dry-run never reads Lark.
        lark_io.split_markdown(markdown)
        if not dry_run:
            existing = [f for f in lark_io.list_folder(folder)
                        if f.get('type') == 'docx' and f.get('name') == draft_title]
            if existing and not replace_draft:
                raise ValueError('Draft already exists: ' + draft_title + '; use --replace-draft only when replacement is authorized')
            if len(existing) > 1:
                raise ValueError('Multiple same-title drafts; specify a unique title instead')
    elif replace_draft:
        raise ValueError('--replace-draft requires a Markdown draft')
    result['backlinks'] = plan_backlinks(list(backlinks), markdown, cases_doc, library_config)
    try:
        for path in files:
            response = lark_io.upload_file(path, folder, dry_run=dry_run)
            token = response.get('file_token', response.get('token'))
            result['files'].append({'name': Path(path).name, 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(), 'response': response, 'token': token, 'readback': False})
            if not dry_run and not token:
                raise lark_io.LarkError('upload_shape', 'Upload did not return file token')
        if markdown is not None:
            response = lark_io.create_doc(draft_title, markdown, folder, dry_run=dry_run)
            result['document'] = {'response': response, 'readback': False, 'actual_title': draft_title,
                                  'action': ('replace_allowed' if replace_draft else 'create_planned') if dry_run
                                            else 'replaced' if existing else 'created',
                                  'collision_check': 'not_executed_dry_run' if dry_run else 'completed'}
            # A same-title document appearing between preflight and create_doc is
            # reported as a conflict; concurrency is not guaranteed by this API.
            if not dry_run and response.get('recovered') and not existing and not replace_draft:
                raise lark_io.LarkError('draft_collision', 'A draft appeared during creation; inspect the returned resource before further writes', detail=response)
            if not dry_run:
                document = response.get('document', response)
                doc_id = document.get('document_id', document.get('token', document.get('url')))
                if not doc_id:
                    raise lark_io.LarkError('doc_shape', 'Creation returned no document address')
                fetched = lark_io.fetch_doc(doc_id)
                if not fetched.get('content'):
                    raise lark_io.LarkError('doc_readback', 'Draft content is empty')
                result['document'].update(readback=True, fetched=fetched)
                listed = lark_io.list_folder(folder)
                addresses = {str(value) for value in (doc_id, document.get('url'), document.get('document_url')) if value}
                match = next((f for f in listed if str(f.get('token', '')) in addresses
                              or (f.get('url') and f['url'] in addresses)), None)
                if match is None or match.get('name') != draft_title:
                    raise lark_io.LarkError('draft_title_readback', 'Draft folder readback did not confirm the requested title', detail=match)
                result['document']['actual_title'] = match['name']
                draft_url = document.get('url', document.get('document_url'))
                for item in result['backlinks']:
                    if not draft_url:
                        # The draft address is never constructed from a token.
                        raise lark_io.LarkError('backlink_no_url', 'Draft creation returned no URL; backlinks not written')
                    if draft_url in lark_io.fetch_doc(item['target']).get('content', ''):
                        item['action'] = 'already_present'
                        continue
                    item['action'] = 'append_unconfirmed'
                    # Appends are never retried; an unknown outcome stays reported.
                    lark_io.run_lark(['docs', '+update', '--doc', item['target'], '--command', 'append',
                                      '--doc-format', 'markdown', '--content', '-'], json_flag=False,
                                     input_text=backlink_markdown(draft_title, draft_url))
                    item['action'] = 'appended'
            else:
                for item in result['backlinks']:
                    item['command'] = lark_io.run_lark(['docs', '+update', '--doc', item['target'], '--command', 'append',
                                                        '--doc-format', 'markdown', '--content', '-'],
                                                       dry_run=True, json_flag=False)['command']
                    item['content_preview'] = backlink_markdown(draft_title, BACKLINK_PLACEHOLDER)
        if not dry_run and result['files']:
            found = set()
            cursor = None
            seen = set()
            while True:
                args = ['drive', 'files', 'list', '--folder-token', folder, '--page-size', '200']
                if cursor:
                    args += ['--page-token', cursor]
                page = lark_io.data(lark_io.run_lark(args))
                found.update(f.get('token') for f in page.get('files', []))
                if page.get('has_more') is False:
                    break
                cursor = page.get('next_page_token', page.get('page_token'))
                if not cursor or cursor in seen:
                    raise lark_io.LarkError('readback_pagination', 'Folder listing is incomplete')
                seen.add(cursor)
            for entry in result['files']:
                entry['readback'] = entry['token'] in found
                if not entry['readback']:
                    raise lark_io.LarkError('file_readback', 'Uploaded file absent from folder listing')
    except lark_io.LarkError as exc:
        result.update(status='partial' if result['files'] or result['document'] else 'failed', error=exc.as_dict())
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('extract', 'inspect-template', 'fill', 'coverage', 'set-default-template', 'upload'):
        p = sub.add_parser(cmd)
        p.add_argument('--out', required=cmd not in ('set-default-template',))
        p.add_argument('--overwrite', action='store_true')
        if cmd == 'extract':
            p.add_argument('--input', required=True)
        if cmd in ('inspect-template', 'fill', 'set-default-template'):
            p.add_argument('--template', required=cmd != 'fill')
        if cmd in ('fill', 'coverage'):
            p.add_argument('--cases', required=True)
        if cmd in ('fill', 'set-default-template'):
            p.add_argument('--mapping')
        if cmd == 'fill':
            p.add_argument('--format', choices=['xlsx','docx','md'])
        if cmd == 'set-default-template':
            p.add_argument('--confirm-default', action='store_true', help='Explicit user request to save default')
        if cmd == 'upload':
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument('--folder')
            group.add_argument('--config')
            p.add_argument('--library', help='Exact configured library name')
            p.add_argument('--folder-key', help='Explicit key in that library folders; never bodies/originals')
            p.add_argument('--file', action='append', default=[])
            p.add_argument('--markdown')
            p.add_argument('--title')
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--replace-draft', action='store_true', help='Explicit authorization to replace one same-title draft')
            p.add_argument('--cases', help='Optional cases.json whose sources and navigation are added to the Markdown draft')
            p.add_argument('--draft-folder-url', help='Explicit link to the draft folder; never derived from a token')
            p.add_argument('--backlink', action='append', default=[],
                           help='Editable draft doc (declared in document.navigation.editable_drafts) to receive a link back')
    args = parser.parse_args(argv)
    inputs = [getattr(args, k, None) for k in ('input','template','cases','mapping','config','markdown')]
    inputs = [p for p in inputs if p] + getattr(args, 'file', [])
    try:
        directory_output = args.command == 'extract'
        if directory_output and Path(args.out).exists():
            directory = Path(args.out)
            if not directory.is_dir() or any(directory.iterdir()):
                raise ValueError('Extraction output must be a new or empty directory')
        out = protect_output(args.out, inputs, args.overwrite or directory_output) if args.out else None
        if args.command == 'extract':
            result_paths = [out / (Path(args.input).name + suffix) for suffix in ('.extract.json', '.extract.md')]
            for target in result_paths:
                protect_output(target, [args.input], args.overwrite)
            report = extract_document(args.input)
            out.mkdir(parents=True, exist_ok=True)
            result_paths[0].write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            result_paths[1].write_text(render_extract(report))
            summary = {'status': 'ok' if report['status'] == 'ok' else 'partial', 'out': str(out)}
        elif args.command == 'fill':
            result_out = out.with_name(out.name + '.result.json')
            protect_output(result_out, inputs, args.overwrite)
            resolved = resolve_template(args.template)
            template = resolved['template']
            mapping_path = args.mapping or resolved['mapping']
            # Built-in/default inputs are also protected from overwrite.
            protect_output(out, [template, mapping_path], args.overwrite)
            mapping = read_json(mapping_path)
            cases = read_json(args.cases)
            fmt = args.format or detect_format(template)
            if fmt in ('xlsx','xlsm'):
                report = fill_xlsx(template if detect_format(template) in ('xlsx','xlsm') else None, cases, mapping, out, args.overwrite)
            elif fmt == 'docx':
                report = fill_docx(template, cases, mapping, out, args.overwrite)
            elif fmt in ('md','html','txt'):
                report = render_markdown(cases, mapping, out, args.overwrite)
            else:
                raise ValueError('Unsupported output format')
            result_out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            summary = {'status': 'ok', 'out': str(out), 'result_out': str(result_out)}
            for key in ('rows_written', 'cases'):
                if key in report:
                    summary[key] = report[key]
            summary['merge_count'] = len(report.get('merges', []))
        elif args.command == 'set-default-template':
            if not args.confirm_default or not args.mapping:
                raise ValueError('Explicit --confirm-default and --mapping required')
            report = set_default_template(args.template, read_json(args.mapping))
            summary = dict(report, status='ok')
            if out:
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        else:
            if args.command == 'inspect-template':
                report = inspect_template(args.template)
            elif args.command == 'coverage':
                cases_doc = read_json(args.cases)
                report = compute_coverage(cases_doc)
                md = out.with_suffix('.md')
                if md == out:
                    raise ValueError('Coverage --out must have a JSON extension')
                protect_output(md, inputs, args.overwrite)
                md.write_text(coverage_markdown(cases_doc, report))
            else:
                if not args.file and not args.markdown:
                    raise ValueError('Provide a file or Markdown draft')
                config = read_json(args.config) if args.config else args.folder
                report = upload_draft(config, args.file, Path(args.markdown).read_text() if args.markdown else None,
                                      args.title, dry_run=args.dry_run, library=args.library, folder_key=args.folder_key,
                                      replace_draft=args.replace_draft,
                                      cases_doc=read_json(args.cases) if args.cases else None,
                                      draft_folder_url=args.draft_folder_url, backlinks=args.backlink)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n')
            summary = {'status': report.get('status', 'ok'), 'out': str(out)}
            if summary['status'] == 'extraction_required':
                summary['status'] = 'partial'
        print(json.dumps(summary, ensure_ascii=False))
        if summary['status'] in ('failed', 'partial'):
            return 3 if args.command == 'upload' else 2
        return 0
    except lark_io.LarkError as exc:
        print(json.dumps({'status': 'failed', 'error': exc.as_dict()}, ensure_ascii=False))
        return 3
    except Exception as exc:
        # Local format/library errors must follow the same JSON/exit-2 contract.
        # LarkError has its own branch above; interrupts are not swallowed.
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
