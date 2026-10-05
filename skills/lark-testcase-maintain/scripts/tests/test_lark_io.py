import json
import subprocess
import unittest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lark_io as io


def reply(value, code=0, stderr=''):
    return subprocess.CompletedProcess([], code, json.dumps(value), stderr)


class TransportTests(unittest.TestCase):
    def test_identity_and_dry_run(self):
        with self.assertRaises(ValueError):
            io.run_lark(['base', '+record-list', '--as=bot'])
        runner = lambda *a, **k: self.fail('dry run invoked runner')
        value = io.run_lark(['drive', '+upload'], runner=runner, dry_run=True)
        self.assertIn('--dry-run', value['command']); self.assertIn('user', value['command'])

    def test_error_envelope_and_redaction(self):
        r = lambda *a, **k: reply({}, 1, json.dumps({'error': {'code': 'permission', 'message': 'denied', 'access_token': 'private'}}))
        with self.assertRaises(io.LarkError) as caught:
            io.run_lark(['base', '+record-list'], runner=r)
        self.assertEqual(caught.exception.code, 'permission')
        self.assertNotIn('private', json.dumps(caught.exception.as_dict()))

    def test_read_retry_write_unknown_no_retry(self):
        calls = []
        def runner(*a, **k):
            calls.append(a); return reply({'ok': False, 'error': {'code': '503', 'message': 'temporary'}})
        with patch.object(io.time, 'sleep'):
            with self.assertRaises(io.LarkError):
                io.run_lark(['base', '+record-list'], runner=runner)
        self.assertEqual(len(calls), 3); calls.clear()
        with self.assertRaises(io.LarkError):
            io.run_lark(['base', '+record-batch-create'], runner=runner)
        self.assertEqual(len(calls), 1)

    def test_matrix_pagination(self):
        responses = [dict(data=[[1]], fields=['N'], record_id_list=['r1'], has_more=True), dict(data=[[2]], fields=['N'], record_id_list=['r2'], has_more=False)]
        with patch.object(io, 'run_lark', side_effect=[{'data': x} for x in responses]) as run:
            result = io.list_records('b', 't', page_size=1)
        self.assertTrue(result['complete']); self.assertEqual(result['records'][1]['fields'], {'N': 2})
        self.assertIn('1', run.call_args.args[0])

    def test_partial_page(self):
        with patch.object(io, 'run_lark', side_effect=[{'data': {'data': [[1]], 'fields': ['N'], 'record_id_list': ['r1'], 'has_more': True}}, io.LarkError('denied', 'no')]):
            result = io.list_records('b', 't')
        self.assertFalse(result['complete']); self.assertEqual(len(result['records']), 1)

    def test_malformed_matrix(self):
        with patch.object(io, 'run_lark', return_value={'data': {'fields': ['a'], 'record_id_list': ['r'], 'data': [[]], 'has_more': False}}):
            self.assertFalse(io.list_records('b', 't')['complete'])

    def test_batch_partial_no_repeat(self):
        with patch.object(io, 'run_lark', side_effect=[{'data': {'record_id_list': ['r'] * 200}}, io.LarkError('timeout', 'unknown')]) as run:
            result = io.batch_create('b', 't', [{'N': i} for i in range(201)])
        self.assertEqual(run.call_count, 2); self.assertEqual(len(result['completed']), 200)
        self.assertTrue(result['unknown_outcome']); self.assertEqual(len(result['failed_slice']), 1)

    def test_batch_update_cell_shape(self):
        with patch.object(io, 'run_lark', return_value={'data': {}}) as run:
            io.batch_update('b', 't', [{'record_id': 'r', 'fields': {'N': 'v'}}])
        args = run.call_args.args[0]
        self.assertEqual(json.loads(args[args.index('--json') + 1]), {'update_records': {'r': {'N': 'v'}}})

    def test_create_stdin_and_fetch(self):
        with patch.object(io, 'run_lark', return_value={'data': {'document': {'content': 'text', 'revision_id': 2}}}):
            self.assertEqual(io.fetch_doc('x')['content'], 'text')
        with patch.object(io, 'run_lark', return_value={'data': {'document': {'url': 'https://example.org/docx/d'}}}) as run:
            io.create_doc('t', 'multi\nline')
        self.assertEqual(run.call_args.kwargs['input_text'], 'multi\nline\n')
        self.assertIn('-', run.call_args.args[0])

    def test_document_search_real_shape_and_limit(self):
        with patch.object(io, 'run_lark', return_value={'data': {'results': [{'result_meta': {'token': 'd'}}], 'has_more': True, 'page_token': 'next'}}):
            result = io.search_docs('x', max_pages=1)
        self.assertFalse(result['complete']); self.assertEqual(len(result['items']), 1)

    def test_upload_uses_original_parent_and_basename(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'original.txt';source.write_text('unchanged')
            with patch.object(io, 'run_lark', return_value={'data': {'url': 'file'}}) as run:
                io.upload_file(source, 'folder')
            args=run.call_args.args[0]
            self.assertEqual(args[args.index('--file')+1], source.name)
            self.assertEqual(run.call_args.kwargs['cwd'], source.parent.resolve())
            self.assertEqual(source.read_text(), 'unchanged')
            self.assertEqual([p.name for p in source.parent.iterdir()], ['original.txt'])

    def test_runner_receives_cwd(self):
        captured=[]
        def runner(command, **kwargs):captured.append(kwargs);return reply({'ok': True})
        io.run_lark(['drive', '+upload'], runner=runner, cwd='/tmp')
        self.assertEqual(captured[0]['cwd'], '/tmp')

    def test_explicit_source_scope_allows_project_reports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.json';cfg.write_text('{}')
            src=root/'sources'/'original.txt';src.parent.mkdir();src.write_text('raw')
            out=io.safe_output(root/'work'/'report.json',[cfg,src],sources=[src])
            self.assertEqual(out,(root/'work'/'report.json').resolve())
            with self.assertRaises(ValueError):io.safe_output(cfg,[cfg],sources=[] ,overwrite=True)
            with self.assertRaises(ValueError):io.safe_output(src.parent/'report.json',[cfg,src],sources=[src])
            with self.assertRaises(ValueError):io.safe_output(root,[cfg],sources=[],directory=True,overwrite=True)

    def test_large_body_is_chunked_without_splitting_tables(self):
        sections=['### TC%d\n\n| 字段 | 原值 |\n| --- | --- |\n| 结果 | 电流 > 9A |\n\n| 测试步骤 | 预期结果 |\n| --- | --- |\n| 操作 | 通过 |' % i for i in range(80)]
        markdown='\n\n'.join(sections)
        with patch.object(io,'list_folder',return_value=[]), patch.object(io,'run_lark',return_value={'data': {'document': {'url': 'https://example.org/docx/d'}}}) as run:
            result=io.create_doc('title',markdown,'folder')
        self.assertGreater(result['body_upload']['chunks'],1)
        self.assertEqual(sum('+create' in c.args[0] for c in run.call_args_list),1)
        for call in run.call_args_list:
            self.assertLessEqual(len(call.kwargs['input_text'].encode()),6001)
        uploaded='\n\n'.join(c.kwargs['input_text'] for c in run.call_args_list if '+update' in c.args[0])
        self.assertEqual(uploaded.count('| 结果 |'),80)
        self.assertEqual(uploaded.count('| 测试步骤 |'),80)

    def test_create_timeout_recovers_in_folder_without_recreating(self):
        doc=dict(name='title',type='docx',url='https://example.org/docx/d',token='d')
        with patch.object(io,'list_folder',side_effect=[[],[doc]]), patch.object(io,'run_lark',side_effect=[io.LarkError('network/timeout','server time out'),{'data': {}}]) as run:
            result=io.create_doc('title','simple body','folder')
        self.assertTrue(result['recovered']);self.assertEqual(sum('+create' in c.args[0] for c in run.call_args_list),1)

    def test_pending_create_not_blindly_repeated(self):
        with patch.object(io,'list_folder',return_value=[]),patch.object(io,'run_lark') as run:
            with self.assertRaises(io.LarkError):io.create_doc('title','body','folder',allow_create=False)
        run.assert_not_called()

    def test_existing_body_reuses_identity(self):
        doc=dict(name='title',type='docx',url='https://example.org/docx/d',token='d')
        with patch.object(io,'list_folder',return_value=[doc]), patch.object(io,'run_lark',return_value={'data': {}}) as run:
            result=io.create_doc('title','body','folder')
        self.assertEqual(result['document']['url'],doc['url']);self.assertFalse(any('+create' in c.args[0] for c in run.call_args_list))

    def test_append_timeout_readback_never_resends(self):
        markdown='\n\n'.join('### TC'+str(i)+'\n\n'+'x'*1000 for i in range(20))
        calls=[]
        def run(args,**kwargs):
            calls.append(args)
            if '--command' in args and args[args.index('--command')+1]=='append':raise io.LarkError('timeout','unknown')
            return {'data': {}}
        with patch.object(io,'run_lark',side_effect=run),patch.object(io,'_committed',return_value=True) as verify:
            result=io.update_doc('url',markdown)
        self.assertEqual(len(calls),result['chunks']);self.assertTrue(verify.called)

    def test_xml_commit_check_preserves_comparison_symbols(self):
        with patch.object(io,'fetch_doc',return_value={'content':'<table><tr><td><p>字段</p></td><td><p>原值</p></td></tr><tr><td><p>结果</p></td><td><p>A &gt; 9A &amp; B &lt; 2</p></td></tr></table>'}):
            self.assertTrue(io._committed('url','| 字段 | 原值 |\n| --- | --- |\n| 结果 | A > 9A & B \\< 2 |'))

    def test_create_timeout_readback_failure_stays_unknown(self):
        with patch.object(io,'list_folder',side_effect=[[],io.LarkError('permission','listing denied')]),patch.object(io,'run_lark',side_effect=io.LarkError('timeout','created maybe')) as run:
            with self.assertRaises(io.LarkError) as caught:io.create_doc('title','body','folder')
        self.assertEqual(caught.exception.code,'document_unknown');self.assertTrue(caught.exception.detail['unknown_outcome']);self.assertEqual(run.call_count,1)

    def test_duplicate_records_within_and_across_pages(self):
        for ids in [['r1','r1'],['r1','r2']]:
            pages=[{'data':dict(items=[dict(record_id=i,fields={}) for i in ids],has_more=True)},
                   {'data':dict(items=[dict(record_id='r1',fields={})],has_more=False)}]
            with patch.object(io,'run_lark',side_effect=pages):result=io.list_records('b','t')
            self.assertFalse(result['complete']);self.assertEqual(result['error']['code'],'pagination')
            self.assertEqual(len(result['records']),0 if ids[0]==ids[1] else 2)

    def test_many_pages_have_linear_id_access(self):
        class CountedRow(dict):
            accesses=0
            def __getitem__(self,key):
                if key=='record_id':type(self).accesses+=1
                return super().__getitem__(key)
        pages=[{'data':dict(items=[CountedRow(record_id='r'+str(i),fields={}) for i in range(start,start+200)],has_more=start<19800)} for start in range(0,20000,200)]
        with patch.object(io,'run_lark',side_effect=pages):result=io.list_records('b','t')
        self.assertTrue(result['complete']);self.assertEqual(len(result['records']),20000);self.assertEqual(result['pages'],100)
        self.assertLessEqual(CountedRow.accesses,40000)
