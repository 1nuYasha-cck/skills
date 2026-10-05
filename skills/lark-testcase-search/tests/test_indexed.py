"""Offline tests for the indexed profile, field maps, per-table bases, publication
scope and section reading. A fake run_lark records every CLI call; nothing is written."""
import contextlib
import copy
import io as stream_io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lark_io as io
import library_model as lm
import search

POLICY = {'default_scope': 'published', 'publication_field': '发布状态', 'published_values': ['已发布']}
BODY = 'https://example.feishu.cn/docx/exampledoc#blockA'


def indexed_config(**extra):
    lib = {'name': 'idx', 'kind': 'standard', 'schema_profile': 'indexed', 'base_token': 'example_search_base',
           'tables': {'cases': 'example_cases', 'catalog': 'example_catalog', 'batches': 'example_batches'},
           'table_base_tokens': {'batches': 'example_maint_base'}, 'search_policy': copy.deepcopy(POLICY)}
    lib.update(extra)
    return {'config_version': '2.0', 'libraries': [lib], 'doc_scopes': []}


def row(rid, case_id, display, status='已发布', **fields):
    base = {'系统用例ID': case_id, '原用例编号': display, '标题': 'title ' + display, '内容版本': '2', '文档版本': '12',
            '正文链接': '[' + BODY + '](' + BODY + ')', '文档token': 'exampledoc', '章节block ID': 'blockA'}
    if status is not None:
        base['发布状态'] = [status]
    base.update(fields)
    return rid, base


def matrix(rows, more=False):
    names = sorted({k for _, f in rows for k in f})
    return {'ok': True, 'data': {'record_id_list': [r for r, _ in rows], 'fields': names,
                                 'data': [[f.get(n) for n in names] for _, f in rows], 'has_more': more}}


class FakeLark:
    """Replays queued responses and keeps every command for assertions."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def flag(self, call, name):
        args = self.calls[call]
        return args[args.index(name) + 1] if name in args else None


class ConfigTests(unittest.TestCase):
    def test_extensions_valid_and_defaults(self):
        search.validate_config(indexed_config(field_maps={'cases': {'原用例编号': '用例号'}}))
        plain = {'name': 'p', 'kind': 'external', 'base_token': 'b', 'tables': {'cases': 't'}}
        self.assertEqual(lm.profile(plain), 'standard')
        self.assertEqual(lm.table_base(plain, 'cases'), 'b')
        self.assertEqual(lm.real_field(plain, '标题'), '标题')

    def test_invalid_extensions_rejected(self):
        bad = [dict(schema_profile='other'), dict(table_base_tokens={'review': 'x'}), dict(table_base_tokens={'cases': ''}),
               dict(field_maps={'cases': {'a': 'x', 'b': 'x'}}), dict(field_maps={'unknown': {'a': 'b'}}),
               dict(search_policy={'default_scope': 'published'}), dict(search_policy={**POLICY, 'default_scope': 'unpublished'}),
               dict(search_policy={**POLICY, 'published_values': []})]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                search.validate_config(indexed_config(**extra))

    def test_example_config_is_valid_and_formal_library_defaults_published(self):
        path = Path(__file__).resolve().parents[1] / 'assets' / 'config.example.json'
        config = search.validate_config(json.loads(path.read_text(encoding='utf-8')))
        indexed = [lib for lib in config['libraries'] if lib.get('schema_profile') == 'indexed']
        self.assertTrue(indexed)
        self.assertEqual(indexed[0]['search_policy']['default_scope'], 'published')


class FindTests(unittest.TestCase):
    def test_field_map_resolves_search_fields_and_keeps_raw_and_logical(self):
        cfg = indexed_config(field_maps={'cases': {'原用例编号': '用例号', '标题': '名称'}}, search_policy=None)
        del cfg['libraries'][0]['search_policy']
        rid, fields = row('rec1', 'uuid-1', 'TC_1')
        fields['用例号'] = fields.pop('原用例编号'); fields['名称'] = fields.pop('标题')
        fake = FakeLark(matrix([(rid, fields)]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(cfg, ['idx'], ['堵转'])
        sent = [fake.calls[0][i + 1] for i, a in enumerate(fake.calls[0]) if a == '--search-field']
        self.assertIn('用例号', sent); self.assertIn('名称', sent); self.assertNotIn('原用例编号', sent)
        run = result['searches'][0]
        self.assertEqual(run['field_map_applied'], {'原用例编号': '用例号', '标题': '名称'})
        self.assertEqual(run['selection_rule'], 'indexed_profile_summary_fields')
        case = result['candidates'][0]
        self.assertEqual(case['display'], {'display_id': 'TC_1', 'title': 'title TC_1', 'label': 'TC_1 title TC_1'})
        self.assertEqual(case['logical_fields']['display_id'], {'field': '用例号', 'canonical': '原用例编号', 'value': 'TC_1'})
        self.assertEqual(case['fields'], fields)
        self.assertEqual(case['identity'], {'record_id': 'rec1', 'case_id': 'uuid-1', 'content_version': '2', 'doc_version': '12'})
        self.assertEqual(case['locator']['record_id'], 'rec1')

    def test_explicit_canonical_search_field_is_mapped(self):
        cfg = indexed_config(field_maps={'cases': {'原用例编号': '用例号'}})
        fake = FakeLark(matrix([]))
        with patch.object(io, 'run_lark', fake):
            search.find_cases(cfg, ['idx'], ['x'], search_fields=['原用例编号', '其他'], scope='all')
        self.assertEqual([fake.calls[0][i + 1] for i, a in enumerate(fake.calls[0]) if a == '--search-field'], ['用例号', '其他'])

    def test_policy_default_published_uses_server_filter_and_local_exact(self):
        # The server returned a draft (text intersects is contains); local check excludes it.
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1'), row('rec2', 'u2', 'TC_2', '草稿')]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(indexed_config(), ['idx'], ['k'])
        self.assertIn('+record-search', fake.calls[0])
        self.assertEqual(json.loads(fake.flag(0, '--filter-json')),
                         {'logic': 'and', 'conditions': [['发布状态', 'intersects', ['已发布']]]})
        self.assertEqual(fake.flag(0, '--base-token'), 'example_search_base')
        self.assertEqual([c['record_id'] for c in result['candidates']], ['rec1'])
        report = result['searches'][0]['filter']
        self.assertEqual((report['scope'], report['source'], report['mode']), ('published', 'policy_default', 'server_intersects_then_local_exact'))
        self.assertEqual(report['excluded'], {'published': 0, 'unpublished': 1, 'unknown': 0})
        self.assertIn('not downloaded', report['note'])

    def test_explicit_all_has_no_filter_and_counts_statuses(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1'), row('rec2', 'u2', 'TC_2', '草稿')]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(indexed_config(), ['idx'], ['k'], scope='all')
        self.assertNotIn('--filter-json', fake.calls[0])
        self.assertEqual(len(result['candidates']), 2)
        report = result['searches'][0]['filter']
        self.assertEqual((report['scope'], report['source']), ('all', 'requested'))
        self.assertEqual(report['status_counts'], {'["已发布"]': 1, '["草稿"]': 1})
        self.assertEqual({c['publication_state'] for c in result['candidates']}, {'published', 'unpublished'})

    def test_unpublished_scope_filters_locally(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1'), row('rec2', 'u2', 'TC_2', '待审核'), row('rec3', 'u3', 'TC_3', None)]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(indexed_config(), ['idx'], ['k'], scope='unpublished')
        self.assertNotIn('--filter-json', fake.calls[0])
        self.assertEqual([c['record_id'] for c in result['candidates']], ['rec2', 'rec3'])
        self.assertEqual(result['searches'][0]['filter']['excluded']['published'], 1)

    def test_unknown_status_never_counts_as_published(self):
        rows = [row('rec1', 'u1', 'TC_1', None), row('rec2', 'u2', 'TC_2', ''), row('rec3', 'u3', 'TC_3')]
        rows[1][1]['发布状态'] = []
        fake = FakeLark(matrix(rows))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(indexed_config(), ['idx'], ['k'])
        self.assertEqual([c['record_id'] for c in result['candidates']], ['rec3'])
        self.assertEqual(result['searches'][0]['filter']['excluded']['unknown'], 2)
        spec = lm.scope_spec(indexed_config()['libraries'][0], 'all')
        self.assertEqual(lm.publication_state(spec, {}), 'unknown')
        self.assertEqual(lm.publication_state(spec, {'发布状态': {'odd': 1}}), 'unknown')
        self.assertEqual(lm.publication_state(spec, {'发布状态': ['已发布', '已归档']}), 'unpublished')

    def test_no_policy_external_not_filtered_and_published_request_isolated(self):
        cfg = {'config_version': '2.0', 'doc_scopes': [], 'libraries': [
            {'name': 'ext', 'kind': 'external', 'base_token': 'example_external', 'tables': {'cases': 'example_t'}},
            indexed_config()['libraries'][0]]}
        rid, fields = row('rec9', 'u9', 'X', '草稿')
        fake = FakeLark(matrix([(rid, fields)]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(cfg, ['ext'], ['k'], search_fields=['标题'])
        self.assertNotIn('--filter-json', fake.calls[0])
        self.assertEqual(len(result['candidates']), 1)
        report = result['searches'][0]['filter']
        self.assertEqual((report['source'], report['mode'], report['scope']), ('no_policy', 'none', 'all'))
        self.assertNotIn('publication_state', result['candidates'][0])
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1')]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(cfg, ['ext', 'idx'], ['k'], search_fields=['标题'], scope='published')
        self.assertEqual(result['status'], 'partial')
        self.assertIn('requires search_policy', result['searches'][0]['error']['message'])
        self.assertTrue(result['searches'][1]['complete'])
        self.assertEqual(len(fake.calls), 1)

    def test_filtered_pagination_partial_keeps_read_rows(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1')], more=True), io.LarkError('permission', 'denied'))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(indexed_config(), ['idx'], ['k'], limit_per_keyword=1)
        self.assertEqual(fake.flag(1, '--offset'), '1')
        self.assertIsNotNone(fake.flag(1, '--filter-json'))
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['remote_complete'])
        self.assertEqual([c['record_id'] for c in result['candidates']], ['rec1'])
        self.assertEqual(result['searches'][0]['error']['code'], 'permission')

    def test_cases_in_other_base_uses_its_token_and_no_wrong_url(self):
        cfg = indexed_config(url='https://example.feishu.cn/base/example_search_base')
        cfg['libraries'][0]['table_base_tokens'] = {'cases': 'example_other_base'}
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1')]))
        with patch.object(io, 'run_lark', fake):
            result = search.find_cases(cfg, ['idx'], ['k'])
        self.assertEqual(fake.flag(0, '--base-token'), 'example_other_base')
        case = result['candidates'][0]
        self.assertEqual(case['locator']['base_token'], 'example_other_base')
        self.assertIsNone(case['url'])

    def test_body_locator_and_read_args_keep_real_link(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1')]))
        with patch.object(io, 'run_lark', fake):
            body = search.find_cases(indexed_config(), ['idx'], ['k'])['candidates'][0]['body']
        self.assertEqual(body['url'], BODY)
        self.assertEqual((body['doc_token'], body['block_id'], body['conflicts']), ('exampledoc', 'blockA', []))
        self.assertEqual(body['read_args'], ['doc-read', '--doc', BODY.split('#')[0], '--block-id', 'blockA',
                                             '--expect-case-id', 'u1', '--expect-content-version', '2', '--expect-doc-revision', '12'])
        roles = lm.role_values(indexed_config()['libraries'][0], row('r', 'u', 'T', **{'文档token': 'otherdoc'})[1])
        conflict = lm.parse_body(roles)
        self.assertEqual(conflict['conflicts'], ['doc_token differs from body link'])
        self.assertNotIn('read_args', conflict)


class LocateTests(unittest.TestCase):
    def test_same_display_id_distinguished_by_case_id(self):
        rows = [row('rec1', 'uuid-a', 'TC_1'), row('rec2', 'uuid-b', 'TC_1'), row('rec3', 'uuid-c', 'TC_10')]
        fake = FakeLark(matrix([rows[1]]), matrix(rows))
        with patch.object(io, 'run_lark', fake):
            result = search.locate_cases(indexed_config(), ['idx'], display_ids=['TC_1'], case_ids=['uuid-b'])
        self.assertEqual(fake.calls[0][fake.calls[0].index('--search-field') + 1], '系统用例ID')
        self.assertEqual(fake.calls[1][fake.calls[1].index('--search-field') + 1], '原用例编号')
        by_case, by_display = result['lookups']
        self.assertEqual([c['record_id'] for c in by_case['exact']], ['rec2'])
        self.assertFalse(by_case['ambiguous'])
        self.assertEqual([c['record_id'] for c in by_display['exact']], ['rec1', 'rec2'])
        self.assertEqual(by_display['distinct_case_ids'], ['uuid-a', 'uuid-b'])
        self.assertTrue(by_display['ambiguous'])
        self.assertEqual(by_display['contains_only'], [{'record_id': 'rec3', 'value': 'TC_10'}])
        self.assertTrue(all(c['display']['display_id'] == 'TC_1' for c in by_display['exact']))

    def test_case_id_needs_indexed_profile(self):
        cfg = {'config_version': '2.0', 'doc_scopes': [], 'libraries': [
            {'name': 'std', 'kind': 'standard', 'base_token': 'b', 'tables': {'cases': 't'}}]}
        with patch.object(io, 'run_lark') as runner:
            result = search.locate_cases(cfg, ['std'], case_ids=['u1'])
        runner.assert_not_called()
        self.assertEqual(result['status'], 'partial')
        self.assertIn('not defined', result['lookups'][0]['error']['message'])

    def test_locate_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'cfg').mkdir(); (root / 'out').mkdir()
            cfg = root / 'cfg' / 'c.json'; cfg.write_text(json.dumps(indexed_config()))
            captured = stream_io.StringIO()
            with contextlib.redirect_stdout(captured), patch.object(io, 'run_lark', FakeLark(matrix([row('rec1', 'u1', 'TC_1')]))):
                code = search.main(['locate', '--config', str(cfg), '--library', 'idx', '--display-id', 'TC_1', '--scope', 'all', '--out', str(root / 'out' / 'l.json')])
            self.assertEqual(code, 0)
            summary = json.loads(captured.getvalue())
            self.assertEqual((summary['lookups_count'], summary['candidates_count']), (1, 1))


class DumpAndLibrariesTests(unittest.TestCase):
    def test_dump_policy_uses_record_list_filter(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1'), row('rec2', 'u2', 'TC_2', '已归档')]))
        with tempfile.TemporaryDirectory() as tmp, patch.object(io, 'run_lark', fake):
            path = Path(tmp) / 'd.ndjson'
            result = search.dump_cases(indexed_config(), 'idx', path)
            self.assertEqual([json.loads(l)['record_id'] for l in path.read_text().splitlines()], ['rec1'])
        self.assertIn('+record-list', fake.calls[0])
        self.assertIsNotNone(fake.flag(0, '--filter-json'))
        self.assertEqual((result['count'], result['filter']['excluded']['unpublished']), (1, 1))

    def test_libraries_reads_every_base_and_reports_counts(self):
        tables = {'ok': True, 'data': {'tables': [{'table_id': 'example_cases'}]}}
        fields = {'ok': True, 'data': {'fields': [{'name': '标题', 'type': 'text'}]}}
        fake = FakeLark(tables, fields, tables, fields, matrix([]), matrix([row('rec1', 'u1', 'TC_1'), row('rec2', 'u2', 'TC_2', None)]))
        with patch.object(io, 'run_lark', fake):
            result = search.list_libraries(indexed_config())
        bases = [fake.flag(i, '--base-token') for i in (0, 2)]
        self.assertEqual(bases, ['example_search_base', 'example_maint_base'])
        lib = result['libraries'][0]
        self.assertEqual(lib['schema_profile'], 'indexed')
        self.assertEqual(lib['table_bases']['batches'], 'example_maint_base')
        self.assertEqual(lib['publication_counts'], {'["已发布"]': 1, '<empty>': 1})
        self.assertEqual(lib['case_count'], 2)
        self.assertNotIn('--filter-json', fake.calls[-1])


SECTION = '<fragment mode="section" requested-start="blockA">\n# u1 / 内容版本 2\n\n显示编号：TC_1\n\n步骤 body\n</fragment>'


def section_response(content=SECTION, revision=12):
    return {'ok': True, 'data': {'document': {'content': content, 'document_id': 'exampledoc', 'revision_id': revision}}}


class SectionReadTests(unittest.TestCase):
    def read(self, fake, doc=BODY, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, patch.object(io, 'run_lark', fake):
            path = Path(tmp) / 'body.md'
            result = search.read_feishu_doc(doc, path, **kwargs)
            return result, path.read_text()

    def test_link_anchor_reads_only_section(self):
        fake = FakeLark(section_response())
        result, text = self.read(fake, expect_case_id='u1', expect_content_version='2', expect_doc_revision='12')
        call = fake.calls[0]
        self.assertEqual(call[:2], ['docs', '+fetch'])
        self.assertEqual((fake.flag(0, '--doc'), fake.flag(0, '--scope'), fake.flag(0, '--start-block-id')),
                         (BODY.split('#')[0], 'section', 'blockA'))
        self.assertEqual((result['read_scope'], result['full_document_read'], result['status']), ('section', False, 'ok'))
        self.assertEqual(result['consistency']['verdict'], 'consistent')
        self.assertEqual(text, SECTION)

    def test_version_mismatch_and_missing_evidence(self):
        result, _ = self.read(FakeLark(section_response(revision=13)), expect_case_id='u1', expect_doc_revision='12')
        self.assertEqual((result['consistency']['verdict'], result['status']), ('mismatch', 'partial'))
        no_heading = SECTION.replace('# u1 / 内容版本 2', 'u1 only in text')
        result, _ = self.read(FakeLark(section_response(no_heading)), expect_case_id='u1', expect_content_version='2')
        self.assertEqual(result['consistency']['checks'], {'case_id': 'found_outside_heading', 'content_version': 'not_found'})
        self.assertEqual(result['consistency']['verdict'], '证据不足')

    def test_non_section_response_rejected(self):
        for content in ('<title>whole</title> full document', '<fragment mode="section" requested-start="other">x</fragment>',
                        '<fragment mode="section" requested-start="blockA">\n</fragment>'):
            with self.subTest(content=content), self.assertRaises(io.LarkError):
                self.read(FakeLark(section_response(content)))

    def test_block_id_conflict_and_checks_need_section(self):
        with self.assertRaises(ValueError):
            self.read(FakeLark(), block_id='blockB')
        with self.assertRaises(ValueError):
            self.read(FakeLark(), doc='exampledoc', expect_case_id='u1')

    def test_full_flag_reads_whole_and_excerpt_is_partial(self):
        fake = FakeLark({'ok': True, 'data': {'document': {'content': '<title>T</title>all', 'revision_id': 12}}})
        result, _ = self.read(fake, full=True)
        self.assertNotIn('--scope', fake.calls[0])
        self.assertEqual((result['read_scope'], result['full_document_read'], result['link_anchor']), ('full', True, 'blockA'))
        excerpt = '<fragment mode="section" requested-start="blockA"><excerpt top-block-id="t">x</excerpt></fragment>'
        result, _ = self.read(FakeLark(section_response(excerpt)))
        self.assertEqual((result['excerpt_only'], result['status']), (True, 'partial'))

    def test_section_cli_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = stream_io.StringIO()
            with contextlib.redirect_stdout(captured), patch.object(io, 'run_lark', FakeLark(section_response())):
                code = search.main(['doc-read', '--doc', 'exampledoc', '--block-id', 'blockA', '--expect-case-id', 'u1',
                                    '--out', str(Path(tmp) / 'b.md')])
            summary = json.loads(captured.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual((summary['read_scope'], summary['block_id'], summary['full_document_read']), ('section', 'blockA', False))
            self.assertEqual(summary['consistency']['verdict'], 'consistent')


RECORD = 'https://example.feishu.cn/base/example_search_base?table=example_cases&record=rec1'


def new_section(heading='TC_1 堵转判定（内容版本 3）', links=None):
    """Section as the publishing Skill writes it: readable heading plus navigation links."""
    links = [('返回用例索引（TC_1）', RECORD), ('返回检索浏览', 'https://example.feishu.cn/base/example_search_base?table=example_cases&view=v1'),
             ('返回正文目录', 'https://example.feishu.cn/docx/examplecatalog')] if links is None else links
    nav = ' · '.join('[' + label + '](' + url + ')' for label, url in links)
    return '<fragment mode="section" requested-start="blockA">\n## ' + heading + '\n\n' + nav + '\n\n### 测试步骤\n\nstep\n</fragment>'


class CrossSkillSectionTests(unittest.TestCase):
    def check(self, content, revision=12, **expect):
        return lm.check_consistency(content, revision, **expect)

    def test_new_heading_backlink_and_bracket_version(self):
        result = self.check(new_section(), expect_record_url=RECORD, expect_version='3', expect_revision='12')
        self.assertEqual(result['checks'], {'record_backlink': 'match', 'content_version': 'match', 'doc_revision': 'match'})
        self.assertEqual((result['verdict'], result['identity_basis']), ('consistent', 'record_backlink'))
        self.assertEqual(lm.heading_version('TC_1 标题（含括号）（内容版本 3）'), '3')

    def test_escaped_backlink_forms_still_match(self):
        escaped = new_section().replace('&record=', '&amp;record=').replace('example_cases', 'example\\_cases')
        self.assertEqual(self.check(escaped, expect_record_url=RECORD)['checks']['record_backlink'], 'match')

    def test_wrong_backlink_is_mismatch(self):
        other = RECORD.replace('rec1', 'rec2')
        content = new_section(links=[('返回用例索引（TC_1）', other)])
        result = self.check(content, expect_record_url=RECORD, expect_version='3')
        self.assertEqual((result['checks']['record_backlink'], result['verdict']), ('mismatch', 'mismatch'))
        prefix = new_section(links=[('返回用例索引', RECORD + '0')])
        self.assertEqual(self.check(prefix, expect_record_url=RECORD)['checks']['record_backlink'], 'mismatch')

    def test_several_or_missing_backlinks_are_insufficient(self):
        both = new_section(links=[('a', RECORD), ('b', RECORD.replace('rec1', 'rec2'))])
        result = self.check(both, expect_record_url=RECORD)
        self.assertEqual((result['checks']['record_backlink'], result['verdict']), ('ambiguous', '证据不足'))
        none = new_section(links=[])
        result = self.check(none, expect_record_url=RECORD, expect_case_id='uuid-1', expect_version='3')
        self.assertEqual(result['checks'], {'record_backlink': 'not_found', 'case_id': 'not_found', 'content_version': 'match'})
        self.assertEqual((result['verdict'], result['identity_basis']), ('证据不足', None))

    def test_version_compared_strictly(self):
        for heading, expected, verdict in (('TC_1 t（内容版本 10）', '1', 'mismatch'), ('TC_1 t（内容版本 1）', '10', 'mismatch'),
                                            ('uuid-1 / 内容版本 10', '1', 'mismatch'), ('uuid-1 / 内容版本 1', '1', 'match')):
            with self.subTest(heading=heading, expected=expected):
                result = self.check(new_section(heading), expect_version=expected)
                self.assertEqual(result['checks']['content_version'], verdict)

    def test_case_id_needs_exact_token(self):
        for heading in ('TC10 / 内容版本 2', 'TC1_2 / 内容版本 2', 'xTC1 / 内容版本 2'):
            with self.subTest(heading=heading):
                result = self.check(new_section(heading, links=[]), expect_case_id='TC1')
                self.assertEqual((result['checks']['case_id'], result['verdict']), ('not_found', '证据不足'))
        self.assertEqual(self.check(new_section('TC1 / 内容版本 2', links=[]), expect_case_id='TC1')['checks']['case_id'], 'match')

    def test_legacy_heading_falls_back_to_case_id(self):
        result = self.check(SECTION, expect_record_url=RECORD, expect_case_id='u1', expect_version='2')
        self.assertEqual(result['checks'], {'record_backlink': 'not_found', 'case_id': 'match', 'content_version': 'match'})
        self.assertEqual((result['verdict'], result['identity_basis']), ('consistent', 'case_id'))
        wrong = self.check(new_section(), expect_record_url=RECORD.replace('rec1', 'rec9'), expect_case_id='u1')
        self.assertEqual(wrong['verdict'], 'mismatch')

    def test_ambiguous_or_wrong_backlink_blocks_case_id_fallback(self):
        both = new_section('TC1 / 内容版本 1', links=[('a', RECORD), ('b', RECORD.replace('rec1', 'rec2'))])
        result = self.check(both, expect_record_url=RECORD, expect_case_id='TC1', expect_version='1')
        self.assertEqual(result['checks'], {'record_backlink': 'ambiguous', 'case_id': 'match', 'content_version': 'match'})
        self.assertEqual((result['verdict'], result['identity_basis']), ('证据不足', None))
        wrong = new_section('TC1 / 内容版本 1', links=[('a', RECORD.replace('rec1', 'rec2'))])
        result = self.check(wrong, expect_record_url=RECORD, expect_case_id='TC1', expect_version='1')
        self.assertEqual((result['checks']['record_backlink'], result['verdict'], result['identity_basis']), ('mismatch', 'mismatch', None))
        unique = new_section('TC1 / 内容版本 1', links=[('a', RECORD)])
        result = self.check(unique, expect_record_url=RECORD, expect_case_id='TC1', expect_version='1')
        self.assertEqual((result['verdict'], result['identity_basis']), ('consistent', 'record_backlink'))
        legacy = new_section('TC1 / 内容版本 1', links=[])
        result = self.check(legacy, expect_record_url=RECORD, expect_case_id='TC1', expect_version='1')
        self.assertEqual((result['checks']['record_backlink'], result['verdict'], result['identity_basis']), ('not_found', 'consistent', 'case_id'))

    def test_read_args_carry_record_url_when_located(self):
        fake = FakeLark(matrix([row('rec1', 'u1', 'TC_1')]))
        with patch.object(io, 'run_lark', fake):
            case = search.find_cases(indexed_config(url='https://example.feishu.cn/base/example_search_base'), ['idx'], ['k'])['candidates'][0]
        args = case['body']['read_args']
        self.assertEqual(args[args.index('--expect-record-url') + 1], case['url'])
        self.assertEqual(lm.record_key(case['url']), ('example_search_base', 'example_cases', 'rec1'))
        self.assertIn('--expect-case-id', args)

    def test_doc_read_cli_record_url_and_invalid_url_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = stream_io.StringIO()
            with contextlib.redirect_stdout(captured), patch.object(io, 'run_lark', FakeLark(section_response(new_section()))):
                code = search.main(['doc-read', '--doc', BODY, '--expect-record-url', RECORD, '--expect-content-version', '3',
                                    '--out', str(Path(tmp) / 'b.md')])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(captured.getvalue())['consistency']['identity_basis'], 'record_backlink')
            fake = FakeLark()
            with patch.object(io, 'run_lark', fake), self.assertRaises(ValueError):
                search.read_feishu_doc(BODY, Path(tmp) / 'c.md', expect_record_url='https://example.feishu.cn/docx/exampledoc')
            self.assertEqual(fake.calls, [])


class RenderTraceTests(unittest.TestCase):
    def result(self):
        return {'query': 'q', 'interpretation': 'i',
                'scope': {'complete': False, 'libraries': ['idx'], 'tables': [], 'documents': [], 'keywords': ['k'], 'failures': [],
                          'unfinished': [], 'filters': [{'library': 'idx', 'scope': 'published', 'excluded': {'unpublished': 1}}]},
                'libraries': [{'name': 'idx', 'match': '高', 'reason': 'r'}],
                'cases': [{'title': '堵转判定', 'display_id': 'TC_1', 'case_id': 'uuid-secret-like', 'record_id': 'rec1',
                           'version': 2, 'body_locator': BODY, 'library': 'idx', 'locator': 'record link', 'match': '高',
                           'reuse': '仅参考', 'reason': 'r', 'differences': [], 'evidence': [{'quote': 'x', 'source': BODY}]}]}

    def test_readable_label_main_and_trace_appendix(self):
        report = search.render_result(self.result())
        main, trace = report.split('## 技术追溯')
        self.assertIn('| TC_1 堵转判定 | idx | 仅参考 |', main)
        self.assertIn('### TC_1 堵转判定', main)
        self.assertNotIn('uuid-secret-like', main)
        self.assertIn('uuid-secret-like', trace)
        self.assertIn('rec1', trace)
        self.assertIn('excluded', main)

    def test_old_results_have_no_trace_section_and_bad_trace_rejected(self):
        result = self.result()
        for key in ('display_id', 'case_id', 'record_id', 'version', 'body_locator'):
            del result['cases'][0][key]
        self.assertNotIn('技术追溯', search.render_result(result))
        for key, value in (('case_id', ''), ('display_id', 3), ('version', None)):
            bad = self.result(); bad['cases'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                search.render_result(bad)


if __name__ == '__main__':
    unittest.main()
