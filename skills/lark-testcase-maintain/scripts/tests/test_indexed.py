import contextlib
import copy
import io as stdio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import indexed as ix
import lark_io as io
import maintain as m

OPTIONS = {('cases', '优先级'): ['P0', 'P1', 'P2', '待确认'], ('cases', '发布状态'): ['草稿', '待审核', '已发布', '已归档'],
           ('cases', '审核状态'): ['未审核', '审核通过', '需修订'], ('review', '审核状态'): ['待审核', '审核通过', '需修订'],
           ('review', '入库状态'): ['未入库', '入库处理中', '已入库', '入库失败'], ('catalog', '状态'): ['启用', '停用'],
           ('batches', '批次状态'): ['待开始', '处理中', '部分完成', '已完成', '失败'], ('routes', '启用状态'): ['启用', '停用']}
WEB = 'https://tenant.example.test'
NAV = '用例双向导航'


class FakeLark:
    """lark-cli stand-in at the transport boundary: Base tables across Bases, Drive and DocxXML documents."""

    def __init__(self, lib):
        self.lib, self.calls, self.dry, self.fail = lib, [], [], []
        self.tables, self.schema, self.docs, self.folders, self.next_id = {}, {}, {}, {}, 0
        self.views = {'vew_full': None, 'vew_browse': ['标题']}
        maps = lib.get('field_maps', {})
        for key, table in lib['tables'].items():
            coordinate = (lib.get('table_base_tokens', {}).get(key, lib['base_token']), table)
            self.tables[coordinate] = []
            fields = [dict(name=maps.get(key, {}).get(n, n if not (key == 'cases' and n == '发布状态') else lib.get('search_policy', {}).get('publication_field', n)), type=t,
                           **({'multiple': False, 'options': [{'name': o} for o in OPTIONS.get((key, n), [])]} if t == 'select' else {}))
                      for n, t in ix.FIELDS[key].items()]
            fields += [dict(name=n, type=t) for n, t in (('创建时间', 'created_at'), ('修改时间', 'updated_at'))]
            self.schema[coordinate] = fields
        self.key_of = {v: k for k, v in lib['tables'].items()}

    def uid(self, prefix):
        self.next_id += 1
        return f'{prefix}{self.next_id:04d}'

    def rows(self, key):
        return self.tables[(self.lib.get('table_base_tokens', {}).get(key, self.lib['base_token']), self.lib['tables'][key])]

    def logical(self, key):
        L = ix.Library(self.lib)
        return [dict(record_id=r['record_id'], fields=L.to_logical(key, r['fields'])) for r in self.rows(key)]

    def set_fields(self, key, record_id, logical):
        L = ix.Library(self.lib)
        for r in self.rows(key):
            if r['record_id'] == record_id:
                r['fields'].update(L.to_real(key, logical))

    def approve(self, display=None):
        for r in self.logical('review'):
            if display in (None, r['fields']['原用例编号']) and ix.plain(r['fields']['审核状态']) == '待审核':
                self.set_fields('review', r['record_id'], {'审核状态': ['审核通过'], '审核人': [{'id': 'ou_reviewer'}], '审核时间': 1700000000000})

    def add_doc(self, title, blocks, folder=None):
        token = 'Doc' + self.uid('X') + 'abcdef'
        self.docs[token] = dict(title=title, blocks=blocks, revision=1)
        if folder:
            self.folders.setdefault(folder, []).append(dict(name=title, type='docx', token=token, url=f'{WEB}/docx/{token}'))
        return token

    def content(self, token):
        doc = self.docs[token]
        return f'<title id="{token}">{doc["title"]}</title>' + ''.join(doc['blocks'])

    def parse(self, xml):
        blocks = []
        for match in re.finditer(r'<(h[1-6]|p|ul|table)\b[^>]*>.*?</\1>', xml, re.S):
            block = match.group()
            if ' id="' not in block.split('>', 1)[0]:
                block = re.sub(r'^<(\w+)', lambda t: f'<{t.group(1)} id="{self.uid("doxcn")}"', block)
            blocks.append(block)
        return blocks

    def arg(self, args, name):
        return args[args.index(name) + 1] if name in args else None

    def __call__(self, args, *, dry_run=False, json_flag=True, input_text=None, cwd=None, **kwargs):
        args = [str(a) for a in args]
        if dry_run:
            self.dry.append(args)
            return {'dry_run': True, 'command': ['lark-cli', *args, '--dry-run']}
        self.calls.append(args)
        for predicate, error, apply in list(self.fail):
            if predicate(args):
                self.fail.remove((predicate, error, apply))
                if apply:
                    self.execute(args, input_text)
                raise error
        return self.execute(args, input_text)

    def execute(self, args, input_text):
        op = args[1]
        base, table = self.arg(args, '--base-token'), self.arg(args, '--table-id')
        if op == '+field-list':
            return {'data': {'fields': copy.deepcopy(self.schema[(base, table)])}}
        if op == '+record-list':
            return {'data': {'records': copy.deepcopy(self.tables[(base, table)]), 'has_more': False}}
        if op == '+record-batch-create':
            body = json.loads(self.arg(args, '--json'))
            ids = []
            for fields in body['create_records']:
                rid = self.uid('rec')
                self.tables[(base, table)].append(dict(record_id=rid, fields=copy.deepcopy(fields))); ids.append(rid)
            return {'data': {'record_id_list': ids}}
        if op == '+record-batch-update':
            body = json.loads(self.arg(args, '--json'))
            for rid, fields in body['update_records'].items():
                row = next(r for r in self.tables[(base, table)] if r['record_id'] == rid)
                row['fields'].update(copy.deepcopy(fields))
            return {'data': {'record_id_list': list(body['update_records'])}}
        if op == '+view-list':
            return {'data': {'views': [dict(id=v, name=v, type='grid') for v in self.views]}}
        if op == '+view-get-visible-fields':
            view = self.arg(args, '--view-id')
            names = [f['name'] for f in self.schema[(base, table)]]
            return {'data': {'visible_fields': names if self.views[view] is None else list(self.views[view])}}
        if op == '+view-set-visible-fields':
            self.views[self.arg(args, '--view-id')] = json.loads(self.arg(args, '--json'))['visible_fields']
            return {'data': {}}
        if op == '+upload':
            return {'data': {'file_token': 'file1', 'url': f'{WEB}/file/file1'}}
        if args[:3] == ['drive', 'files', 'list']:
            return {'data': {'files': copy.deepcopy(self.folders.get(self.arg(args, '--folder-token'), [])), 'has_more': False}}
        if op == '+fetch':
            token = ix.doc_token(self.arg(args, '--doc')) or self.arg(args, '--doc')
            return {'data': {'document': {'content': self.content(token), 'revision_id': self.docs[token]['revision']}}}
        if op == '+create':
            token = self.add_doc(self.arg(args, '--title'), self.parse(input_text), self.arg(args, '--parent-token'))
            return {'data': {'document': {'document_id': token, 'url': f'{WEB}/docx/{token}'}}}
        if op == '+update':
            token = ix.doc_token(self.arg(args, '--doc')) or self.arg(args, '--doc')
            doc = self.docs[token]
            revision = self.arg(args, '--revision-id')
            if revision is not None and int(revision) != doc['revision']:
                raise io.LarkError('revision_conflict', 'document changed')
            blocks = self.parse(input_text)
            if self.arg(args, '--command') == 'append':
                doc['blocks'] += blocks
            else:
                anchor = self.arg(args, '--block-id')
                index = next(i for i, b in enumerate(doc['blocks']) if f'id="{anchor}"' in b.split('>', 1)[0])
                doc['blocks'][index + 1:index + 1] = blocks
            doc['revision'] += 1
            return {'data': {'document': {'revision_id': doc['revision']}, 'result': 'success'}}
        raise AssertionError('unexpected command ' + ' '.join(args))

    def writes(self):
        return [c for c in self.calls if c[1] in ('+record-batch-create', '+record-batch-update', '+create', '+update', '+upload', '+view-set-visible-fields')]


def library(**extra):
    lib = dict(name='正式库', kind='standard', schema_profile='indexed', base_token='baseSearch',
               tables=dict(catalog='tblCatalog', cases='tblIndex', routes='tblRoutes', sources='tblSources', batches='tblBatches', issues='tblIssues', review='tblReview'),
               table_base_tokens=dict(sources='baseMaint', batches='baseMaint', issues='baseMaint', review='baseMaint'),
               field_maps=dict(cases={'标题': '用例名称'}, review={'用例标题': '标题文本'}),
               folders=dict(originals='fldOrig', bodies='fldBodies'),
               search_policy=dict(default_scope='published', publication_field='发布状态', published_values=['已发布']),
               maintain=dict(web_url=WEB, catalog_doc='', browse=dict(view='vew_browse', full_view='vew_full', fields=['标题', '原用例编号', '摘要', '正文链接']),
                             volume=dict(max_cases=30, max_chars=60000), navigation_headings=[NAV]))
    lib.update(extra)
    return lib


class IndexedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'cases.xlsx'; self.source.write_bytes(b'original bytes')
        self.lib = library()
        self.fake = FakeLark(self.lib)
        catalog = self.fake.add_doc('正文目录', ['<h1 id="doxcnCat">用例正文目录</h1>'])
        self.lib['maintain']['catalog_doc'] = f'{WEB}/docx/{catalog}'
        self.catalog_doc = catalog
        self.fake.rows('routes').append(dict(record_id='recRoute', fields={'Base token': 'baseSearch', 'Table ID': 'tblIndex', '启用状态': ['启用'], '目标记录上限': 1000}))
        self.cfg = dict(config_version='2.0', libraries=[self.lib])
        patcher = patch.object(io, 'run_lark', side_effect=self.fake); patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(io.time, 'sleep'); patcher.start(); self.addCleanup(patcher.stop)

    def plan(self, cases=None, **extra):
        plan = dict(plan_version='2.0', library='正式库', batch=dict(name='b', source_file=str(self.source), note='梳理说明'), targets=dict(base=True, doc=True),
                    categories=[{'分类路径': '示例模块/示例功能', '分类说明': '示例功能相关', '关键词': '示例功能'}],
                    cases=cases or [self.case('TC_001', '示例功能判定'), self.case('TC_002', '示例功能清零')])
        plan.update(extra)
        return plan

    def case(self, number, title, **extra):
        return dict({'用例编号': number, '用例标题': title, '所属分类': '示例模块/示例功能', '项目': '示例项目', '模块': '示例模块', '功能': '示例功能',
                     '测试步骤': ['操作一', '操作二'], '预期结果': ['结果一'], '优先级': 'P1', '来源位置': f'示例功能!A{number[-1]}:F{number[-1]}',
                     '扩展字段': {'执行记录': '待填写'}}, **extra)

    def import_and_approve(self, plan=None):
        result = m.run_import(self.cfg, plan or self.plan())
        self.assertEqual(result['status'], 'ok', result)
        self.fake.approve()
        return result

    def published(self):
        return {ix.plain(r['fields']['原用例编号']): r for r in self.fake.logical('cases') if ix.plain(r['fields'].get('发布状态')) == '已发布'}

    # ---- import

    def test_import_cross_base_pending_review_never_publishes(self):
        result = m.run_import(self.cfg, self.plan())
        self.assertEqual(result['status'], 'ok', result); self.assertFalse(result['published'])
        writes = self.fake.writes()
        bases = {(self.fake.arg(c, '--base-token'), self.fake.arg(c, '--table-id')) for c in writes if c[0] == 'base'}
        self.assertEqual(bases, {('baseMaint', 'tblSources'), ('baseMaint', 'tblBatches'), ('baseMaint', 'tblReview')})
        self.assertEqual(self.fake.rows('cases'), []); self.assertEqual(self.fake.rows('catalog'), [])
        review = self.fake.rows('review')
        self.assertEqual(len(review), 2)
        self.assertIn('标题文本', review[0]['fields']); self.assertNotIn('用例标题', review[0]['fields'])
        self.assertEqual(review[0]['fields']['审核状态'], ['待审核']); self.assertEqual(review[0]['fields']['入库状态'], ['未入库'])
        self.assertTrue(all(o['review_locator']['base_token'] == 'baseMaint' for o in result['operations']))
        batch = self.fake.logical('batches')[0]['fields']
        self.assertEqual(batch['批次状态'], ['已完成']); self.assertEqual(batch['总用例数'], 2)
        again = m.run_import(self.cfg, self.plan())
        self.assertEqual(again['counts']['unchanged'], 2); self.assertEqual(len(self.fake.rows('review')), 2)
        self.assertEqual(sum(c[1] == '+upload' for c in self.fake.calls), 1)

    def test_import_dry_run_sends_no_remote_writes(self):
        result = m.run_import(self.cfg, self.plan(), dry_run=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.fake.writes(), []); self.assertTrue(self.fake.dry)

    def test_original_number_never_overwrites_other_system_id(self):
        self.import_and_approve()
        moved = self.plan(cases=[self.case('TC_001', '另一来源同编号', 来源位置='其他!A9:F9')])
        result = m.run_import(self.cfg, moved)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['counts']['conflict'], 1)
        self.assertEqual(len(self.fake.rows('review')), 2)
        allowed = m.run_import(self.cfg, dict(moved, allow_duplicate_display_ids=['TC_001']))
        self.assertEqual(allowed['status'], 'ok', allowed); self.assertEqual(allowed['counts']['create'], 1)
        ids = {ix.plain(r['fields']['系统用例ID']) for r in self.fake.logical('review') if r['fields']['原用例编号'] == 'TC_001'}
        self.assertEqual(len(ids), 2)

    def test_missing_schema_and_config_block_writes(self):
        coordinate = ('baseMaint', 'tblReview')
        self.fake.schema[coordinate] = [f for f in self.fake.schema[coordinate] if f['name'] != '提交键']
        result = m.run_import(self.cfg, self.plan())
        self.assertEqual(result['status'], 'failed'); self.assertIn('提交键', result['input_error']); self.assertEqual(self.fake.writes(), [])
        broken = copy.deepcopy(self.lib); del broken['tables']['review']
        with self.assertRaises(ValueError):
            m.library_config(dict(config_version='2.0', libraries=[broken]), '正式库')
        for bad in (dict(field_maps={'cases': {'不存在': 'x'}}), dict(table_base_tokens={'unknown': 'b'}), dict(base_token='<base>')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.library_config(dict(config_version='2.0', libraries=[dict(copy.deepcopy(self.lib), **bad)]), '正式库')
        standard = dict(name='s', kind='standard', base_token='b', tables=dict(catalog='c', cases='k', batches='t'), folders=dict(root='r', originals='o', bodies='d'), field_maps={'cases': {}})
        with self.assertRaises(ValueError):
            m.library_config(dict(config_version='2.0', libraries=[standard]), 's')

    # ---- publish gating

    def test_pending_review_cannot_publish(self):
        m.run_import(self.cfg, self.plan())
        before = len(self.fake.writes())
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok'); self.assertEqual(result['published'], [])
        self.assertEqual(len(result['pending_review']), 2); self.assertEqual(result['rejected'], [])
        review = self.fake.logical('review')
        self.fake.set_fields('review', review[0]['record_id'], {'审核状态': ['需修订']})
        self.fake.set_fields('review', review[1]['record_id'], {'审核状态': ['审核通过']})
        again = m.publish_library(self.cfg, '正式库')
        self.assertEqual(again['published'], [])
        reasons = sorted(' '.join(r['reasons']) for r in again['rejected'])
        self.assertIn('not approved: 需修订', reasons[1]); self.assertIn('lacks reviewer', reasons[0])
        self.assertEqual(len(self.fake.writes()), before)

    def test_publish_plan_cannot_carry_review_status(self):
        self.import_and_approve()
        before = len(self.fake.writes())
        result = ix.publish(self.lib, dict(publish_version='1.0', system_ids=[], 审核状态='审核通过'))
        self.assertEqual(result['status'], 'failed'); self.assertIn('review status is always read from Base', result['input_error'])
        self.assertEqual(len(self.fake.writes()), before)

    def test_change_after_approval_is_refused(self):
        self.import_and_approve()
        review = self.fake.logical('review')
        self.fake.set_fields('review', review[0]['record_id'], {'测试步骤': '人工改过的步骤'})
        self.fake.set_fields('review', review[1]['record_id'], {'审核绑定内容哈希': 'f' * 64})
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['published'], [])
        reasons = ' '.join(' '.join(r['reasons']) for r in result['rejected'])
        self.assertIn('content changed after submission', reasons); self.assertIn('审核绑定内容哈希', reasons)
        self.assertFalse(any(c[1] in ('+update', '+create') and c[0] == 'docs' for c in self.fake.calls))

    def test_missing_tenant_url_refuses_without_guessing(self):
        self.import_and_approve()
        self.lib['maintain']['web_url'] = ''
        before = len(self.fake.writes())
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'failed'); self.assertIn('web_url', result['input_error'])
        self.assertEqual(len(self.fake.writes()), before)

    def test_publish_dry_run_sends_no_remote_writes(self):
        self.import_and_approve()
        before = len(self.fake.writes())
        result = m.publish_library(self.cfg, '正式库', dry_run=True)
        self.assertEqual(result['status'], 'ok', result); self.assertEqual(len(result['plan']), 2)
        self.assertEqual(len(self.fake.writes()), before)

    # ---- publish behaviour

    def test_publish_bidirectional_links_and_version_sync(self):
        self.import_and_approve()
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        published = self.published(); self.assertEqual(set(published), {'TC_001', 'TC_002'})
        record = published['TC_001']; f = record['fields']
        doc = f['文档token']; content = self.fake.content(doc)
        self.assertTrue(f['正文链接'].startswith('[TC_001 示例功能判定]('))
        self.assertIn(f'{WEB}/docx/{doc}#{f["章节block ID"]}', f['正文链接'])
        self.assertIn(f'id="{f["章节block ID"]}">TC_001 示例功能判定（内容版本 1）</h2>', content)
        self.assertIn(f'record={record["record_id"]}', content); self.assertIn('返回用例索引（TC_001）', content)
        self.assertIn('返回检索浏览', content); self.assertIn('返回正文目录', content)
        self.assertNotIn(f['系统用例ID'], content)
        self.assertEqual({r['fields']['文档版本'] for r in published.values()}, {str(self.fake.docs[doc]['revision'])})
        self.assertIn(f'{WEB}/docx/{doc}', self.fake.content(self.catalog_doc))
        review = {r['fields']['原用例编号']: r['fields'] for r in self.fake.logical('review')}
        self.assertEqual(review['TC_001']['入库状态'], ['已入库'])
        self.assertTrue(review['TC_001']['正式索引链接'].endswith('record=' + record['record_id']))
        self.assertEqual(ix.plain(f['用例名称'] if '用例名称' in f else f['标题']), '示例功能判定')
        self.assertEqual(len(self.fake.rows('catalog')), 1)
        self.assertFalse(m.check_library(self.cfg, '正式库')['issues'])
        again = m.publish_library(self.cfg, '正式库')
        self.assertEqual(again['status'], 'ok'); self.assertEqual(again['published'], [])

    def test_existing_volume_keeps_anchors_navigation_and_syncs_revision(self):
        old = self.fake.add_doc('旧分卷', ['<h1 id="doxcnOld1">旧用例 A</h1>', '<p id="doxcnOldP">正文</p>', f'<h1 id="doxcnNav">{NAV}</h1>',
                                         f'<table id="doxcnTbl"><tr><td><p id="doxcnCell">TC_000 <a href="{WEB}/base/baseSearch?table=tblIndex&amp;record=recOld">返回用例索引</a></p></td></tr></table>'], 'fldBodies')
        self.fake.rows('cases').append(dict(record_id='recOld', fields={'系统用例ID': 'legacy-1', '原用例编号': 'TC_000', '用例名称': '旧用例', '项目': '示例项目', '模块': '示例模块', '功能点': '示例功能',
                                                                       '发布状态': ['已发布'], '文档token': old, '章节block ID': 'doxcnOld1', '文档版本': '1', '正文链接': f'[{WEB}/docx/{old}#doxcnOld1]({WEB}/docx/{old}#doxcnOld1)'}))
        before_ids = re.findall(r'id="([^"]+)"', self.fake.content(old))
        self.import_and_approve()
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        content = self.fake.content(old)
        self.assertTrue(all(f'id="{i}"' in content for i in before_ids))
        self.assertLess(content.index('TC_002 示例功能清零'), content.index(NAV)); self.assertLess(content.index('旧用例 A'), content.index('TC_001 示例功能判定'))
        self.assertTrue(content.rstrip().endswith('</table>'))
        revision = str(self.fake.docs[old]['revision'])
        self.assertTrue(all(r['fields']['文档版本'] == revision for r in self.fake.logical('cases')))
        legacy = [i for i in m.check_library(self.cfg, '正式库')['issues'] if i.get('locator', {}).get('record_id') == 'recOld']
        self.assertFalse([i for i in legacy if 'link back' in i['issue'] or 'anchor' in i['issue']], legacy)

    def test_revision_keeps_old_section_and_requires_new_approval(self):
        self.import_and_approve(); m.publish_library(self.cfg, '正式库')
        first = self.published()['TC_001']['fields']
        sid = first['系统用例ID']
        revised = self.plan(cases=[self.case('TC_001', '示例功能判定（修订）', 系统用例ID=sid)])
        result = m.run_import(self.cfg, revised)
        self.assertEqual(result['counts']['revise'], 1, result); self.assertEqual(result['operations'][0]['version'], 2)
        blocked = m.publish_library(self.cfg, '正式库')
        self.assertEqual(blocked['published'], []); self.assertEqual(len(blocked['pending_review']), 1)
        self.assertEqual(self.published()['TC_001']['fields']['内容版本'], '1')
        self.fake.approve('TC_001')
        done = m.publish_library(self.cfg, '正式库')
        self.assertEqual(done['status'], 'ok', done)
        now = self.published()['TC_001']['fields']
        self.assertEqual(now['内容版本'], '2'); self.assertEqual(now['文档token'], first['文档token'])
        self.assertNotEqual(now['章节block ID'], first['章节block ID'])
        content = self.fake.content(first['文档token'])
        self.assertIn(f'id="{first["章节block ID"]}">TC_001 示例功能判定（内容版本 1）', content)
        self.assertIn('TC_001 示例功能判定（修订）（内容版本 2）', content)
        self.assertEqual(len([r for r in self.fake.logical('cases') if r['fields']['系统用例ID'] == sid]), 1)

    def test_body_failure_leaves_index_unpublished(self):
        self.import_and_approve()
        self.fake.fail.append((lambda a: a[:2] == ['docs', '+update'] and self.fake.arg(a, '--doc') != self.catalog_doc, io.LarkError('permission', 'no edit permission'), False))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertEqual(result['published'], [])
        self.assertEqual(self.published(), {})
        staged = self.fake.logical('cases')
        self.assertTrue(staged and all(ix.plain(r['fields']['发布状态']) == '待审核' for r in staged))
        self.assertTrue(all(ix.plain(r['fields']['入库状态']) == '未入库' for r in self.fake.logical('review')))
        retry = m.publish_library(self.cfg, '正式库')
        self.assertEqual(retry['status'], 'ok', retry); self.assertEqual(set(self.published()), {'TC_001', 'TC_002'})
        self.assertEqual(len(self.fake.rows('cases')), 2)

    def test_unknown_body_write_is_not_retried(self):
        self.import_and_approve()
        self.fake.fail.append((lambda a: a[:2] == ['docs', '+update'] and self.fake.arg(a, '--doc') != self.catalog_doc, io.LarkError('timeout', 'timed out'), False))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertIn('unknown_outcome', result)
        self.assertEqual(result['body_error']['code'], 'document_unknown')
        section_writes = [c for c in self.fake.calls if c[:2] == ['docs', '+update'] and self.fake.arg(c, '--doc') != self.catalog_doc]
        self.assertEqual(len(section_writes), 1); self.assertEqual(self.published(), {})

    def test_unknown_body_write_that_applied_is_recovered_by_readback(self):
        self.import_and_approve()
        self.fake.fail.append((lambda a: a[:2] == ['docs', '+update'] and self.fake.arg(a, '--doc') != self.catalog_doc, io.LarkError('timeout', 'timed out'), True))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result); self.assertEqual(set(self.published()), {'TC_001', 'TC_002'})
        token = self.published()['TC_001']['fields']['文档token']
        self.assertEqual(self.fake.content(token).count('TC_001 示例功能判定（内容版本 1）'), 1)

    def test_unknown_index_write_is_not_retried(self):
        self.import_and_approve()
        self.fake.fail.append((lambda a: a[1] == '+record-batch-update' and self.fake.arg(a, '--table-id') == 'tblIndex' and '已发布' in self.fake.arg(a, '--json'), io.LarkError('timeout', 'timed out'), False))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertIn('unknown_outcome', result)
        publishes = [c for c in self.fake.calls if c[1] == '+record-batch-update' and self.fake.arg(c, '--table-id') == 'tblIndex' and '已发布' in self.fake.arg(c, '--json')]
        self.assertEqual(len(publishes), 1)
        self.assertEqual({r['status'] for r in result['unknown_outcome_readback']['cases']}, {'待审核'})

    def test_review_change_during_publish_blocks_index(self):
        self.import_and_approve()
        original = ix.fetch_blocks
        def tamper(doc):
            result = original(doc)
            if any(c[:2] == ['docs', '+update'] for c in self.fake.calls):
                for r in self.fake.logical('review'):
                    self.fake.set_fields('review', r['record_id'], {'审核状态': ['需修订']})
            return result
        with patch.object(ix, 'fetch_blocks', side_effect=tamper):
            result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertEqual(self.published(), {})
        self.assertTrue(any('review changed during publish' in r['reasons'][0] for r in result['rejected']))

    def test_volume_thresholds_split_without_cutting_cases(self):
        self.lib['maintain']['volume'] = dict(max_cases=2, max_chars=60000)
        cases = [self.case(f'TC_00{i}', f'用例{i}') for i in range(1, 6)]
        self.import_and_approve(self.plan(cases=cases))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        volumes = {}
        for r in self.published().values():
            volumes.setdefault(r['fields']['文档token'], []).append(r['fields']['原用例编号'])
        self.assertEqual(sorted(len(v) for v in volumes.values()), [1, 2, 2])
        titles = sorted(self.fake.docs[t]['title'] for t in volumes)
        self.assertEqual(titles, ['正式库 示例模块/示例功能 第1卷', '正式库 示例模块/示例功能 第2卷', '正式库 示例模块/示例功能 第3卷'])
        for token, numbers in volumes.items():
            for number in numbers:
                self.assertEqual(self.fake.content(token).count(f'>{number} '), 1)

    def section_size(self, number, title):
        L = ix.Library(self.lib)
        case = m.normalized(dict(self.case(number, title), 来源文件=self.source.name))
        links = [(f'返回用例索引（{number}）', L.record_url('cases', 'rec0000')), ('返回检索浏览', L.browse_url()), ('返回正文目录', L.catalog_url())]
        return len(ix.section_xml(case, '1', links))

    def test_char_threshold_opens_new_volume(self):
        self.lib['maintain']['volume'] = dict(max_cases=30, max_chars=int(self.section_size('TC_001', '一') * 1.6))
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '一'), self.case('TC_002', '二')]))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len({r['fields']['文档token'] for r in self.published().values()}), 2)

    def test_missing_navigation_config_is_reported(self):
        self.lib['maintain'].pop('browse'); self.lib['maintain']['catalog_doc'] = ''
        self.import_and_approve()
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result); self.assertEqual(len(result['missing_navigation_config']), 2)
        content = self.fake.content(self.published()['TC_001']['fields']['文档token'])
        self.assertNotIn('返回检索浏览', content); self.assertIn('返回用例索引', content)

    def test_route_capacity_blocks_publish(self):
        self.fake.rows('routes')[0]['fields']['目标记录上限'] = 1
        self.import_and_approve()
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'failed'); self.assertIn('capacity', result['input_error'])

    # ---- browse view, check, export

    def test_browse_view_sets_only_target_view(self):
        dry = m.browse_view(self.cfg, '正式库', dry_run=True)
        self.assertEqual(dry['status'], 'ok'); self.assertEqual(self.fake.writes(), [])
        result = m.browse_view(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result); self.assertTrue(result['full_view']['complete'])
        self.assertEqual(self.fake.views['vew_browse'], ['用例名称', '原用例编号', '摘要', '正文链接'])
        self.assertIsNone(self.fake.views['vew_full'])
        self.assertEqual([c for c in self.fake.writes()], [c for c in self.fake.writes() if self.fake.arg(c, '--view-id') == 'vew_browse'])
        self.lib['maintain']['browse']['fields'].append('系统用例ID')
        with self.assertRaises(ValueError):
            m.browse_view(self.cfg, '正式库')

    def test_check_and_export_cover_registered_tables_and_bodies(self):
        self.import_and_approve(); m.publish_library(self.cfg, '正式库')
        record = self.published()['TC_001']
        doc = record['fields']['文档token']
        self.fake.docs[doc]['revision'] += 1
        issues = m.check_library(self.cfg, '正式库')['issues']
        self.assertTrue(any(i['issue'] == 'document revision out of sync' for i in issues))
        target = Path(self.tmp.name) / 'backup'
        result = m.export_library(self.cfg, '正式库', target)
        self.assertEqual(result['status'], 'ok', result)
        names = {f['path'] for f in result['files']}
        self.assertTrue({'catalog.ndjson', 'cases.ndjson', 'routes.ndjson', 'sources.ndjson', 'batches.ndjson', 'issues.ndjson', 'review.ndjson', 'body-0001.xml'} <= names)
        row = json.loads((target / 'cases.ndjson').read_text().splitlines()[0])
        self.assertIn('用例名称', row['fields']); self.assertIn('标题', row['logical']); self.assertEqual(row['locator']['base_token'], 'baseSearch')
        self.fake.fail.append((lambda a: a[1] == '+fetch', io.LarkError('permission', 'denied'), False))
        partial = m.export_library(self.cfg, '正式库', Path(self.tmp.name) / 'partial')
        self.assertEqual(partial['status'], 'partial'); self.assertFalse(partial['complete'])
        self.fake.fail.append((lambda a: a[1] == '+record-list' and self.fake.arg(a, '--table-id') == 'tblReview', io.LarkError('permission', 'denied'), False))
        checked = m.check_library(self.cfg, '正式库')
        self.assertEqual(checked['status'], 'partial'); self.assertIn('review', checked['read_errors'])

    def test_legacy_rows_are_reported_not_rehashed(self):
        self.fake.rows('review').append(dict(record_id='recLegacy', fields={'系统用例ID': 'legacy', '原用例编号': 'TC_9', '结构化用例': '{"schema_version":"1.0"}', '审核状态': ['审核通过'], '入库状态': ['已入库'], '内容版本': '2'}))
        checked = m.check_library(self.cfg, '正式库')
        self.assertEqual(checked['info']['legacy_review_rows'], 1); self.assertFalse(checked['issues'])
        self.fake.set_fields('review', 'recLegacy', {'入库状态': ['未入库']})
        result = m.publish_library(self.cfg, '正式库')
        self.assertIn('legacy', ' '.join(result['rejected'][0]['reasons']))

    def test_cli_publish_requires_indexed_and_reads_plan(self):
        config = Path(self.tmp.name) / 'config.json'; config.write_text(json.dumps(self.cfg, ensure_ascii=False))
        out = Path(self.tmp.name) / 'out' / 'publish.json'
        with patch.object(sys, 'argv', ['maintain.py', 'publish', '--config', str(config), '--library', '正式库', '--out', str(out), '--dry-run']):
            with contextlib.redirect_stdout(stdio.StringIO()) as printed:
                self.assertEqual(m.main(), 0)
        self.assertEqual(json.loads(printed.getvalue())['status'], 'ok')
        self.assertTrue(out.exists()); self.assertEqual(self.fake.writes(), [])

    # ---- first review fixes

    def duplicate_plan(self):
        return self.plan(cases=[self.case('TC_001', '同名用例', 来源位置='甲!A1:F1'), self.case('TC_001', '同名用例', 来源位置='乙!A1:F1')],
                         allow_duplicate_display_ids=['TC_001'])

    def test_duplicate_readable_heading_gets_its_own_section(self):
        self.import_and_approve(self.duplicate_plan())
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        rows = [r for r in self.fake.logical('cases') if r['fields']['原用例编号'] == 'TC_001']
        self.assertEqual(len({r['fields']['系统用例ID'] for r in rows}), 2)
        anchors = [r['fields']['章节block ID'] for r in rows]
        self.assertEqual(len(set(anchors)), 2)
        content = self.fake.content(rows[0]['fields']['文档token'])
        for r in rows:
            owned = ix.owned_sections(content, 'TC_001 同名用例（内容版本 1）', ix.Library(self.lib).record_url('cases', r['record_id']))
            self.assertEqual([h['id'] for h in owned], [r['fields']['章节block ID']])
        self.assertFalse(m.check_library(self.cfg, '正式库')['issues'])

    def test_unknown_write_never_adopts_another_records_section(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '同名用例', 来源位置='甲!A1:F1')]))
        self.assertEqual(m.publish_library(self.cfg, '正式库')['status'], 'ok')
        first = self.published()['TC_001']
        later = self.plan(cases=[self.case('TC_001', '同名用例', 来源位置='乙!A1:F1')], allow_duplicate_display_ids=['TC_001'])
        self.assertEqual(m.run_import(self.cfg, later)['status'], 'ok'); self.fake.approve()
        self.fake.fail.append((lambda a: a[:2] == ['docs', '+update'] and self.fake.arg(a, '--doc') != self.catalog_doc, io.LarkError('timeout', 'timed out'), False))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertEqual(result['body_error']['code'], 'document_unknown')
        staged = [r for r in self.fake.logical('cases') if r['record_id'] != first['record_id']]
        self.assertEqual([ix.plain(r['fields']['发布状态']) for r in staged], ['待审核'])
        self.assertFalse(staged[0]['fields'].get('章节block ID'))
        retry = m.publish_library(self.cfg, '正式库')
        self.assertEqual(retry['status'], 'ok', retry)
        anchors = {r['fields']['章节block ID'] for r in self.fake.logical('cases')}
        self.assertEqual(len(anchors), 2); self.assertFalse(m.check_library(self.cfg, '正式库')['issues'])

    def test_identity_and_provenance_tampering_after_approval_is_refused(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        record = self.fake.logical('review')[0]
        for column, value in (('系统用例ID', 'changed-id'), ('内容版本', '999'), ('来源ID', 'other-source'), ('来源文件链接', WEB + '/file/other'),
                              ('导入批次ID', 'batch-other'), ('待确认项', '["新增疑问"]')):
            with self.subTest(column=column):
                saved = copy.deepcopy(self.fake.rows('review')[0]['fields'])
                self.fake.set_fields('review', record['record_id'], {column: value})
                state = ix.review_state(ix.Library(self.lib), self.fake.logical('review')[0])
                self.assertTrue(any('identity or provenance' in p for p in state['problems']), state['problems'])
                result = m.publish_library(self.cfg, '正式库', dict(publish_version='1.0', system_ids=[ix.plain(self.fake.logical('review')[0]['fields']['系统用例ID'])]))
                self.assertEqual(result['published'], [])
                self.fake.rows('review')[0]['fields'] = saved
        structured = json.loads(record['fields']['结构化用例']); structured['identity']['source_id'] = 'forged'
        self.fake.set_fields('review', record['record_id'], {'结构化用例': json.dumps(structured, ensure_ascii=False)})
        state = ix.review_state(ix.Library(self.lib), self.fake.logical('review')[0])
        self.assertTrue(any('structured identity' in p for p in state['problems']))
        self.assertEqual(self.published(), {})

    def test_ledger_repair_after_index_commit_and_review_update_failure(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        self.fake.fail.append((lambda a: a[1] == '+record-batch-update' and self.fake.arg(a, '--table-id') == 'tblReview', io.LarkError('permission', 'denied'), False))
        first = m.publish_library(self.cfg, '正式库')
        self.assertEqual(first['status'], 'partial'); self.assertIn('TC_001', self.published())
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')
        doc_writes = sum(c[:2] == ['docs', '+update'] for c in self.fake.calls)
        second = m.publish_library(self.cfg, '正式库')
        self.assertEqual(second['status'], 'ok', second)
        self.assertEqual([r['status'] for r in second['ledger_repairs']], ['repaired'])
        review = self.fake.logical('review')[0]['fields']
        index = self.published()['TC_001']
        self.assertEqual(ix.plain(review['入库状态']), '已入库'); self.assertEqual(review['章节block ID'], index['fields']['章节block ID'])
        self.assertTrue(review['正式索引链接'].endswith('record=' + index['record_id']))
        self.assertEqual(sum(c[:2] == ['docs', '+update'] for c in self.fake.calls), doc_writes)
        third = m.publish_library(self.cfg, '正式库')
        self.assertEqual(third['status'], 'ok'); self.assertEqual(third['plan'], [])

    def test_ledger_repair_respects_withdrawn_review_and_body_check(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        self.fake.fail.append((lambda a: a[1] == '+record-batch-update' and self.fake.arg(a, '--table-id') == 'tblReview', io.LarkError('permission', 'denied'), False))
        m.publish_library(self.cfg, '正式库')
        review = self.fake.logical('review')[0]
        self.fake.set_fields('review', review['record_id'], {'审核状态': ['需修订']})
        withdrawn = m.publish_library(self.cfg, '正式库')
        self.assertEqual(withdrawn['published'], []); self.assertIn('not valid', withdrawn['rejected'][0]['reasons'][0])
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')
        self.fake.set_fields('review', review['record_id'], {'审核状态': ['审核通过']})
        token = self.published()['TC_001']['fields']['文档token']
        self.fake.docs[token]['blocks'] = [b for b in self.fake.docs[token]['blocks'] if 'record=' not in b]
        broken = m.publish_library(self.cfg, '正式库')
        self.assertEqual(broken['status'], 'partial'); self.assertEqual(broken['ledger_repairs'][0]['status'], 'failed')
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')

    def test_duplicate_numbers_in_plan_need_explicit_allow_and_distinct_ids(self):
        plan = self.duplicate_plan(); plan.pop('allow_duplicate_display_ids')
        self.assertIn('duplicate 用例编号', m.run_import(self.cfg, plan)['input_error'])
        same = self.plan(cases=[self.case('TC_001', '同名用例', 来源位置='甲!A1:F1'), self.case('TC_001', '同名用例', 来源位置='甲!A1:F1')], allow_duplicate_display_ids=['TC_001'])
        result = m.run_import(self.cfg, same)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['counts']['conflict'], 1); self.assertEqual(self.fake.writes(), [])

    def test_check_flags_shared_or_misattributed_sections(self):
        self.import_and_approve(); m.publish_library(self.cfg, '正式库')
        rows = self.fake.logical('cases')
        self.fake.set_fields('cases', rows[1]['record_id'], {'章节block ID': rows[0]['fields']['章节block ID'], '正文链接': rows[0]['fields']['正文链接']})
        issues = {i['issue'] for i in m.check_library(self.cfg, '正式库')['issues']}
        self.assertIn('several index records share one body section', issues)
        self.assertIn('body section does not link back to its index record', issues)

    # ---- second review: content verification and hard volume limits

    def test_single_case_larger_than_max_chars_is_rejected(self):
        self.lib['maintain']['volume'] = dict(max_cases=30, max_chars=200)
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '一')]))
        before = len(self.fake.writes())
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['published'], []); self.assertIn('never split', result['rejected'][0]['reasons'][0])
        self.assertEqual(len(self.fake.writes()), before)

    def test_revision_moves_to_new_volume_when_old_is_full(self):
        self.lib['maintain']['volume'] = dict(max_cases=2, max_chars=60000)
        self.import_and_approve(); m.publish_library(self.cfg, '正式库')
        first = self.published()['TC_001']['fields']
        revised = self.plan(cases=[self.case('TC_001', '堵转判定（修订）', 系统用例ID=first['系统用例ID'])])
        m.run_import(self.cfg, revised); self.fake.approve('TC_001')
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        now = self.published()['TC_001']['fields']
        self.assertNotEqual(now['文档token'], first['文档token'])
        self.assertIn(f'id="{first["章节block ID"]}"', self.fake.content(first['文档token']))
        self.assertEqual(ix.case_sections(ix.fetch_blocks(first['文档token'])), 2)
        self.assertFalse(m.check_library(self.cfg, '正式库')['issues'])

    def test_max_cases_counts_retained_historical_sections(self):
        self.lib['maintain']['volume'] = dict(max_cases=3, max_chars=60000)
        self.import_and_approve(); m.publish_library(self.cfg, '正式库')
        first = self.published()['TC_001']['fields']
        m.run_import(self.cfg, self.plan(cases=[self.case('TC_001', '修订', 系统用例ID=first['系统用例ID'])])); self.fake.approve()
        m.publish_library(self.cfg, '正式库')
        self.assertEqual(ix.case_sections(ix.fetch_blocks(first['文档token'])), 3)
        m.run_import(self.cfg, self.plan(cases=[self.case('TC_003', '新增')])); self.fake.approve()
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotEqual(self.published()['TC_003']['fields']['文档token'], first['文档token'])
        self.assertEqual(ix.case_sections(ix.fetch_blocks(first['文档token'])), 3)

    def test_existing_full_same_title_volume_is_never_overwritten(self):
        self.lib['maintain']['volume'] = dict(max_cases=1, max_chars=60000)
        full = self.fake.add_doc('正式库 示例模块/示例功能 第1卷', ['<h2 id="doxcnPrev">TC_X 旧（内容版本 1）</h2>', '<p id="doxcnPrevP">旧正文</p>'], 'fldBodies')
        before = list(self.fake.docs[full]['blocks'])
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '一')]))
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.fake.docs[full]['blocks'], before)
        token = self.published()['TC_001']['fields']['文档token']
        self.assertNotEqual(token, full); self.assertTrue(self.fake.docs[token]['title'].endswith('第2卷'))

    def test_recovery_rejects_section_with_modified_content(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '一')])); m.publish_library(self.cfg, '正式库')
        record = self.published()['TC_001']
        L = ix.Library(self.lib)
        state = ix.review_state(L, self.fake.logical('review')[0])
        token = record['fields']['文档token']
        self.fake.docs[token]['blocks'] = [b.replace('操作二', '被人改过') for b in self.fake.docs[token]['blocks']]
        blocks = ix.fetch_blocks(token)
        with self.assertRaises(io.LarkError) as raised:
            ix.insert_section(L, token, state['case'], state['version'], [], blocks, L.record_url('cases', record['record_id']))
        self.assertEqual(raised.exception.code, 'section_conflict')
        self.assertIn('body section content differs from reviewed version', {i['issue'] for i in m.check_library(self.cfg, '正式库')['issues']})

    def ledger_failure(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '一')]))
        self.fake.fail.append((lambda a: a[1] == '+record-batch-update' and self.fake.arg(a, '--table-id') == 'tblReview', io.LarkError('permission', 'denied'), False))
        self.assertEqual(m.publish_library(self.cfg, '正式库')['status'], 'partial')

    def test_ledger_repair_refuses_modified_body_content(self):
        self.ledger_failure()
        token = self.published()['TC_001']['fields']['文档token']
        self.fake.docs[token]['blocks'] = [b.replace('结果一', '截断') for b in self.fake.docs[token]['blocks']]
        result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['ledger_repairs'][0]['status'], 'failed'); self.assertIn('reviewed content', result['ledger_repairs'][0]['error'])
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')

    def test_ledger_repair_rereads_review_before_writing(self):
        self.ledger_failure()
        original = ix.fetch_blocks
        def withdraw(doc):
            result = original(doc)
            for r in self.fake.logical('review'):
                self.fake.set_fields('review', r['record_id'], {'审核状态': ['需修订']})
            return result
        with patch.object(ix, 'fetch_blocks', side_effect=withdraw):
            result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['ledger_repairs'][0]['error'], 'review changed before ledger repair')
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')

    # ---- final review: structured publish inputs are bound

    def tamper_structured(self, record_id, mutate):
        row = next(r for r in self.fake.logical('review') if r['record_id'] == record_id)
        structured = json.loads(row['fields']['结构化用例']); mutate(structured)
        self.fake.set_fields('review', record_id, {'结构化用例': json.dumps(structured, ensure_ascii=False)})

    def test_structured_publish_inputs_tampering_is_refused(self):
        mutations = {
            '来源位置': lambda d: d['case'].update(来源位置='OTHER!A99:F99'),
            '来源文件': lambda d: d['case'].update(来源文件='other.xlsx'),
            '分类路径': lambda d: d['category'].update(分类路径='其他/分类'),
            '分类说明': lambda d: d['category'].update(分类说明='改过的说明'),
            '关键词': lambda d: d['category'].update(关键词='改过'),
            'provenance': lambda d: d['provenance'].update(range='Z1'),
            'agent_notes': lambda d: d.update(agent_notes=['伪造']),
        }
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        record_id = self.fake.logical('review')[0]['record_id']
        for name, mutate in mutations.items():
            with self.subTest(field=name):
                saved = copy.deepcopy(self.fake.rows('review')[0]['fields'])
                self.tamper_structured(record_id, mutate)
                state = ix.review_state(ix.Library(self.lib), self.fake.logical('review')[0])
                self.assertTrue(any('identity or provenance' in p for p in state['problems']), state['problems'])
                before = len(self.fake.writes())
                result = m.publish_library(self.cfg, '正式库')
                self.assertEqual(result['published'], []); self.assertEqual(len(self.fake.writes()), before)
                self.fake.rows('review')[0]['fields'] = saved
        self.assertFalse(ix.review_state(ix.Library(self.lib), self.fake.logical('review')[0])['problems'])

    def test_structured_change_during_publish_blocks_index(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        record_id = self.fake.logical('review')[0]['record_id']
        original = ix.fetch_blocks
        def tamper(doc):
            result = original(doc)
            if any(c[:2] == ['docs', '+update'] for c in self.fake.calls):
                self.tamper_structured(record_id, lambda d: d['case'].update(来源位置='OTHER!A99:F99'))
            return result
        with patch.object(ix, 'fetch_blocks', side_effect=tamper):
            result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['status'], 'partial'); self.assertEqual(self.published(), {})
        self.assertTrue(any('review changed during publish' in r['reasons'][0] for r in result['rejected']))
        self.assertEqual(self.fake.rows('catalog'), [])

    def test_structured_change_before_ledger_repair_blocks_repair(self):
        self.ledger_failure()
        record_id = self.fake.logical('review')[0]['record_id']
        original = ix.fetch_blocks
        def tamper(doc):
            result = original(doc)
            self.tamper_structured(record_id, lambda d: d['category'].update(分类说明='改过'))
            return result
        with patch.object(ix, 'fetch_blocks', side_effect=tamper):
            result = m.publish_library(self.cfg, '正式库')
        self.assertEqual(result['ledger_repairs'][0]['status'], 'failed')
        self.assertEqual(ix.plain(self.fake.logical('review')[0]['fields']['入库状态']), '未入库')

    def test_reimport_is_idempotent_but_changed_location_or_category_is_new_version(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        sid = self.fake.logical('review')[0]['fields']['系统用例ID']
        again = m.run_import(self.cfg, self.plan(cases=[self.case('TC_001', '堵转判定')]))
        self.assertEqual(again['counts']['unchanged'], 1); self.assertEqual(len(self.fake.rows('review')), 1)
        moved = m.run_import(self.cfg, self.plan(cases=[self.case('TC_001', '堵转判定', 系统用例ID=sid, 来源位置='新表!A2:F2')]))
        self.assertEqual(moved['counts']['revise'], 1); self.assertEqual(moved['operations'][0]['version'], 2)
        plan = self.plan(cases=[self.case('TC_001', '堵转判定', 系统用例ID=sid, 来源位置='新表!A2:F2')])
        plan['categories'][0]['分类说明'] = '新说明'
        recat = m.run_import(self.cfg, plan)
        self.assertEqual(recat['counts']['revise'], 1); self.assertEqual(recat['operations'][0]['version'], 3)
        latest = max(self.fake.logical('review'), key=lambda r: int(r['fields']['内容版本']))
        self.assertEqual(ix.plain(latest['fields']['审核状态']), '待审核')

    def test_rows_without_full_binding_stay_legacy(self):
        self.import_and_approve(self.plan(cases=[self.case('TC_001', '堵转判定')]))
        record_id = self.fake.logical('review')[0]['record_id']
        self.tamper_structured(record_id, lambda d: d.update(hash_scheme='maintain-indexed-2'))
        state = ix.review_state(ix.Library(self.lib), self.fake.logical('review')[0])
        self.assertTrue(any('legacy' in p for p in state['problems']))
        self.assertEqual(m.publish_library(self.cfg, '正式库')['published'], [])
        self.assertEqual(m.check_library(self.cfg, '正式库')['info']['legacy_review_rows'], 1)
        again = m.run_import(self.cfg, self.plan(cases=[self.case('TC_001', '堵转判定')]))
        self.assertEqual(again['counts']['revise'], 1)
        latest = max(self.fake.logical('review'), key=lambda r: int(r['fields']['内容版本']))
        self.assertEqual(ix.plain(latest['fields']['审核状态']), '待审核')

    def test_standard_profile_unchanged_with_search_policy(self):
        standard = dict(name='s', kind='standard', schema_profile='standard', base_token='b', tables=dict(catalog='c', cases='k', batches='t'),
                        folders=dict(root='r', originals='o', bodies='d'), search_policy=dict(default_scope='published'))
        self.assertIs(m.library_config(dict(config_version='2.0', libraries=[standard]), 's'), standard)
        self.assertFalse(m.is_indexed(standard))
        with self.assertRaises(ValueError):
            m.publish_library(dict(config_version='2.0', libraries=[standard]), 's')
        with self.assertRaises(ValueError):
            m.library_config(dict(config_version='2.0', libraries=[dict(standard, schema_profile='other')]), 's')


if __name__ == '__main__':
    unittest.main()
