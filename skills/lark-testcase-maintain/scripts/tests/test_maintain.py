import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import maintain as m


class Library:
    def __init__(self):
        self.rows={k: [] for k in m.TABLE_NAMES}; self.writes=0; self.docs={}; self.uploads=0; self.corrupt=False
    def fields(self,base,table):return {'fields':m.STANDARD_TABLES[table]}
    def read(self,base,table):
        rows=copy.deepcopy(self.rows[table])
        if self.corrupt and table=='cases' and rows:rows[0]['fields']['用例标题']='changed by another editor'
        return dict(records=rows,complete=True,pages=1,error=None)
    def create(self,base,table,rows,*,dry_run=False):
        if dry_run:return dict(status="ok",command=["preview"])
        self.writes+=1
        for row in rows:self.rows[table].append(dict(record_id='r'+str(len(self.rows[table])),fields=copy.deepcopy(row)))
        return dict(status='ok',completed=rows,results=[])
    def update(self,base,table,rows,*,dry_run=False):
        if dry_run:return dict(status="ok",command=["preview"])
        self.writes+=1
        for change in rows:
            for r in self.rows[table]:
                if r['record_id']==change['record_id']:r['fields'].update(copy.deepcopy(change['fields']))
        return dict(status='ok',completed=rows,results=[])
    def upload(self,*args,dry_run=False):
        if dry_run:return dict(command=['preview'])
        self.uploads+=1;return {'url':'https://example.org/file/source'}
    def doc(self,title,body,*args,**kwargs):
        url='https://example.org/docx/'+str(len(self.docs));self.docs[url]=body;return {'url':url}
    def update_doc(self,url,body,**kwargs):
        self.docs[url]=body;return dict(document=dict(url=url),chunks=1)
    def fetch(self,url,**kwargs):return {'content':self.docs[url]}


class MaintainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Path(self.tmp.name)/'source.md';self.source.write_text('original')
        self.cfg=dict(config_version='2.0',libraries=[dict(name='test',kind='standard',base_token='b',tables={k:k for k in m.TABLE_NAMES},folders=dict(root='root',originals='orig',bodies='body'))])
        self.plan=dict(plan_version='2.0',library='test',batch=dict(name='batch',source_file=str(self.source)),targets=dict(base=True,doc=True),categories=[{'分类路径':'module'}],cases=[dict(用例编号='TC1',用例标题='title',所属分类='module',预期结果='expected',来源位置='行1',扩展字段={'note':'unchanged'})])
        self.remote=Library()
        for name, fn in [('list_fields',self.remote.fields),('list_records',self.remote.read),('batch_create',self.remote.create),('batch_update',self.remote.update),('upload_file',self.remote.upload),('create_doc',self.remote.doc),('fetch_doc',self.remote.fetch),('update_doc',self.remote.update_doc)]:
            p=patch.object(m.io,name,side_effect=fn);p.start();self.addCleanup(p.stop)

    def test_import_missing_version_and_repeat(self):
        result=m.run_import(self.cfg,self.plan);self.assertEqual(result['status'],'ok',result)
        stored=self.remote.rows['cases'][0]['fields'];self.assertEqual(stored['适用版本'],'');self.assertEqual(stored['审核状态'],['待审核'])
        result=m.run_import(self.cfg,self.plan);self.assertEqual(result['counts']['unchanged'],1);self.assertEqual(len(self.remote.rows['cases']),1);self.assertEqual(self.remote.uploads,1)
        self.assertEqual(self.source.read_text(),'original')

    def test_conflict_fail_no_write(self):
        m.run_import(self.cfg,self.plan);self.plan['cases'][0]['用例标题']='new';self.plan['on_conflict']='fail'
        before=self.remote.writes;result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.remote.writes,before)

    def test_skip_body_preserves_existing(self):
        m.run_import(self.cfg,self.plan);self.plan['cases'][0]['用例标题']='new';self.plan['on_conflict']='skip'
        result=m.run_import(self.cfg,self.plan);self.assertEqual(result['status'],'ok')
        self.assertNotIn('### TC1 new',list(self.remote.docs.values())[-1])

    def test_reclassification_updates_old_category(self):
        m.run_import(self.cfg,self.plan);self.plan['categories']=[{'分类路径':'other'}];self.plan['cases'][0]['所属分类']='other'
        result=m.run_import(self.cfg,self.plan);self.assertEqual(result['status'],'ok',result)
        counts={r['fields']['分类路径']:r['fields']['用例数'] for r in self.remote.rows['catalog']}
        self.assertEqual(counts,{'module':0,'other':1})

    def test_deprecated_excluded_from_body(self):
        m.run_import(self.cfg,self.plan);self.plan['cases'][0]['状态']='已废弃';result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'ok');self.assertEqual(self.remote.rows['catalog'][0]['fields']['用例数'],0)
        self.assertNotIn('### TC1',list(self.remote.docs.values())[-1])

    def test_readback_change_is_partial(self):
        self.remote.corrupt=True;result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'partial');self.assertTrue(result['readback']['errors'])

    def test_incomplete_read_blocks_all_writes(self):
        with patch.object(m.io,'list_records',return_value=dict(records=[],complete=False,pages=0,error={'message':'missing page'})):
            result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.remote.writes,0);self.assertEqual(self.remote.uploads,0)

    def test_dry_run_no_writes(self):
        result=m.run_import(self.cfg,self.plan,dry_run=True)
        self.assertEqual(result['status'],'ok');self.assertEqual(self.remote.uploads+self.remote.writes,0)

    def test_unknown_key_and_duplicate_and_empty_targets(self):
        plan=copy.deepcopy(self.plan);plan['cases'][0]['extra']='unknown';plan['cases'].append(plan['cases'][0]);plan['targets']=dict(base=False,doc=False)
        errors=m.validate_plan(plan,{'cases':m.CASES});self.assertGreaterEqual(len(errors),3)
        self.assertFalse(m.validate_plan(self.plan,{'cases':m.CASES}))

    def test_hash_ignores_metadata_preserves_newlines(self):
        a=self.plan['cases'][0];b=dict(a,审核状态='已审核',来源文件='other',正文文档='url')
        self.assertEqual(m.content_hash(a),m.content_hash(b))
        self.assertNotEqual(m.content_hash(a),m.content_hash(dict(a,预期结果='expected\n')))

    def test_document_only(self):
        self.plan['targets']['base']=False;r=m.run_import(self.cfg,self.plan)
        self.assertEqual(r['status'],'ok',r);self.assertEqual(len(self.remote.rows['cases']),0);self.assertEqual(len(self.remote.docs),1)

    def test_check_and_export(self):
        m.run_import(self.cfg,self.plan);self.assertFalse(m.check_library(self.cfg,'test')['issues'])
        self.remote.rows['cases'][0]['fields']['用例标题']='manual'
        self.assertTrue(m.check_library(self.cfg,'test')['issues'])
        target=Path(self.tmp.name)/'backup';r=m.export_library(self.cfg,'test',target)
        self.assertEqual(r['status'],'ok');self.assertTrue((target/'manifest.json').exists());self.assertEqual(len(r['files']),4)

    def test_nonstandard_refused(self):
        self.cfg['libraries'][0]['kind']='external'
        with self.assertRaises(ValueError):m.library_config(self.cfg,'test')

    def test_wrong_schema_blocks_writes(self):
        wrong=copy.deepcopy(m.STANDARD_TABLES['cases']);wrong[0]['type']='number'
        def fields(base,table):return {'fields':wrong if table=='cases' else m.STANDARD_TABLES[table]}
        with patch.object(m.io,'list_fields',side_effect=fields):r=m.run_import(self.cfg,self.plan)
        self.assertEqual(r['status'],'failed');self.assertEqual(self.remote.uploads+self.remote.writes,0)

    def test_batch_partial_reads_without_repeating(self):
        with patch.object(m.io,'batch_create',return_value=dict(status='partial',unknown_outcome=True,completed=[],results=[])) as create:
            result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'partial');self.assertEqual(create.call_count,1)
        self.assertIn('unknown_outcome_readback',result)

    def test_export_page_failure_preserves_manifest(self):
        with patch.object(m.io,'list_records',return_value=dict(records=[],complete=False,pages=0,error={'code':'failure'})):
            result=m.export_library(self.cfg,'test',Path(self.tmp.name)/'partial')
        self.assertEqual(result['status'],'partial');self.assertFalse(result['complete'])

    def test_init_dry_run_no_remote_requests(self):
        with patch.object(m.io,'run_lark',wraps=m.io.run_lark) as run:
            result=m.init_library('library',dry_run=True)
        self.assertEqual(result['status'],'ok');self.assertTrue(all(c.kwargs.get('dry_run') for c in run.call_args_list))

    def test_init_success_and_partial_resource_inventory(self):
        with patch.object(m.io,'create_folder',side_effect=[{'token':'root'},{'token':'orig'},{'token':'body'}]), patch.object(m.io,'create_base',return_value={'base_token':'base'}), patch.object(m.io,'create_table',side_effect=[{'table_id':k} for k in m.TABLE_NAMES]):
            result=m.init_library('library')
        self.assertEqual(result['status'],'ok');self.assertEqual(len(result['resources']),7)
        with patch.object(m.io,'create_folder',side_effect=[{'token':'root'},m.io.LarkError('permission','denied')]):
            result=m.init_library('library')
        self.assertEqual(result['status'],'partial');self.assertEqual(len(result['resources']),1)

    def test_check_reports_invalid_extension_without_crashing(self):
        m.run_import(self.cfg,self.plan);self.remote.rows['cases'][0]['fields']['扩展字段']='bad json'
        result=m.check_library(self.cfg,'test')
        self.assertTrue(any(x['issue']=='invalid extension JSON' for x in result['issues']))

    def test_unchanged_skips_body_and_update_keeps_url(self):
        first=m.run_import(self.cfg,self.plan);self.assertEqual(first['status'],'ok')
        url=self.remote.rows['catalog'][0]['fields']['正文文档']
        with patch.object(m.io,'create_doc',side_effect=AssertionError('unexpected create')),patch.object(m.io,'update_doc',side_effect=AssertionError('unexpected body update')):
            second=m.run_import(self.cfg,self.plan)
        self.assertEqual(second['status'],'ok');self.assertEqual(len(self.remote.docs),1)
        self.plan['cases'][0]['用例标题']='changed'
        third=m.run_import(self.cfg,self.plan);self.assertEqual(third['status'],'ok')
        self.assertEqual(self.remote.rows['catalog'][0]['fields']['正文文档'],url)
        self.assertEqual(len(self.remote.docs),1);self.assertIn('changed',self.remote.docs[url])

    def test_failed_body_keeps_batch_and_reuses_source(self):
        with patch.object(m.io,'create_doc',side_effect=m.io.LarkError('permission','no')):
            first=m.run_import(self.cfg,self.plan)
        self.assertEqual(first['status'],'partial');self.assertTrue(first['batch_audit']['confirmed'])
        batches=self.remote.rows['batches'];self.assertEqual(batches[0]['fields']['执行结果'],'部分完成')
        self.assertEqual(batches[0]['fields']['原件链接'],'https://example.org/file/source')
        self.assertTrue(batches[0]['fields']['原件SHA256'])
        second=m.run_import(self.cfg,self.plan);self.assertEqual(second['status'],'ok')
        self.assertEqual(self.remote.uploads,1)
        self.assertEqual(self.remote.rows['cases'][0]['fields']['入库批次'],batches[0]['fields']['批次号'])

    def test_markdown_keeps_values_and_separates_tables(self):
        case=dict(self.plan['cases'][0],测试步骤='电流 > 9A & 温度 < 20',预期结果='A|B\n第二行')
        rendered=m.render_category_markdown({'分类路径':'module'},[case],'test')
        self.assertNotIn('&gt;',rendered);self.assertNotIn('&lt;',rendered);self.assertIn(' > 9A & ',rendered)
        self.assertIn('\\< 20',rendered);self.assertIn('A\\|B<br>第二行',rendered)
        self.assertIn('\n\n| 测试步骤 |',rendered);self.assertNotIn('| 模块 |  |',rendered)

    def test_local_body_size_error_does_not_mark_unknown_creation(self):
        self.plan['cases'][0]['预期结果']='x'*7000
        result=m.run_import(self.cfg,self.plan)
        self.assertEqual(result['status'],'partial')
        note=json.loads(self.remote.rows['batches'][0]['fields']['说明'])
        self.assertEqual(note['body_attempts'],[])

    def test_confirmed_missing_body_releases_all_prior_attempts(self):
        calls=[]
        def create(title,body,parent,*,allow_create=True):
            calls.append(allow_create)
            if len(calls)==1:
                raise m.io.LarkError('document_unknown','creation timeout',detail={'unknown_outcome':True})
            if not allow_create:
                raise m.io.LarkError('document_unknown','no confirmed document')
            return self.remote.doc(title,body,parent)
        with patch.object(m.io,'create_doc',side_effect=create):
            first=m.run_import(self.cfg,self.plan);second=m.run_import(self.cfg,self.plan)
            self.assertEqual(first['status'],'partial');self.assertEqual(second['status'],'partial')
            self.assertEqual(calls,[True,False]);self.assertEqual(len(self.remote.docs),0)
            self.assertEqual(second['next_steps']['title'],'test module')
            # Follow the documented procedure: backup, complete folder read,
            # explicit confirmation of absence, resolve ALL matching attempts.
            backup=m.export_library(self.cfg,'test',Path(self.tmp.name)/'backup')
            self.assertTrue(backup['complete'])
            with patch.object(m.io,'list_folder',return_value=[]) as folder:
                self.assertEqual(m.io.list_folder('body'),[])
            sha=second['next_steps']['source_sha256'];title=second['next_steps']['title']
            for row in self.remote.rows['batches']:
                if row['fields']['原件SHA256']!=sha:continue
                note=json.loads(row['fields']['说明'])
                for attempt in note['body_attempts']:
                    if attempt['title']==title and not attempt.get('confirmed'):
                        attempt.update(failed_known=True,resolution={'confirmed_missing':True,'evidence':'complete folder read and explicit confirmation'})
                self.remote.update('b','batches',[dict(record_id=row['record_id'],fields={'说明':json.dumps(note)})])
            third=m.run_import(self.cfg,self.plan);fourth=m.run_import(self.cfg,self.plan)
        self.assertEqual(third['status'],'ok',third);self.assertEqual(fourth['status'],'ok',fourth)
        self.assertEqual(calls,[True,False,True]);self.assertEqual(len(self.remote.docs),1);self.assertEqual(self.remote.uploads,1)
        self.assertTrue(all(json.loads(row['fields']['说明'])['body_attempts'][0].get('failed_known') for row in self.remote.rows['batches'][:2]))


    def array_case(self):
        return dict(self.plan['cases'][0], 测试步骤=['操作一', '操作二\n原文'], 预期结果=['观察一', '观察二'], 需求编号=['REQ-2','REQ-1','REQ-2'], 设计方法=['边界值','等价类'])

    def test_array_text_normalization_and_hash_equivalence(self):
        raw=self.array_case();before=copy.deepcopy(raw)
        text=dict(raw,测试步骤='1. 操作一\n2. 操作二\n原文',预期结果='1. 观察一\n2. 观察二',需求编号='REQ-2、REQ-1、REQ-2',设计方法='边界值、等价类')
        normalized=m.normalized(raw)
        for key in m.ARRAY_TEXT:self.assertEqual(normalized[key],text[key])
        self.assertEqual(m.content_hash(raw),m.content_hash(text));self.assertEqual(raw,before)
        self.assertNotEqual(m.content_hash(raw),m.content_hash(dict(raw,需求编号=list(reversed(raw['需求编号'][:-1])))))
        self.assertNotEqual(m.content_hash(raw),m.content_hash(dict(raw,测试步骤=['操作二','操作一'])))

    def test_array_import_dry_run_readback_and_text_repeat(self):
        self.plan['cases']=[self.array_case()];before=copy.deepcopy(self.plan)
        self.assertEqual(m.validate_plan(self.plan,{'cases':m.CASES}),[])
        preview=m.run_import(self.cfg,self.plan,dry_run=True)
        self.assertEqual(preview['status'],'ok',preview);self.assertEqual(self.remote.writes+self.remote.uploads,0)
        first=m.run_import(self.cfg,self.plan);self.assertEqual(first['status'],'ok',first)
        stored=self.remote.rows['cases'][0]['fields']
        for key in m.ARRAY_TEXT:
            self.assertIsInstance(stored[key],str);self.assertEqual(stored[key],preview['operations'][0]['case'][key])
        self.assertEqual(stored['内容哈希'],m.content_hash(self.plan['cases'][0]))
        body=next(iter(self.remote.docs.values()));self.assertIn('1. 操作一<br>2. 操作二<br>原文',body);self.assertIn('REQ-2、REQ-1、REQ-2',body)
        self.assertEqual(self.plan,before);self.assertFalse(m.check_library(self.cfg,'test')['issues'])
        self.plan['cases']=[dict(self.plan['cases'][0],**{key:stored[key] for key in m.ARRAY_TEXT})]
        with patch.object(m.io,'create_doc',side_effect=AssertionError('unexpected create')),patch.object(m.io,'update_doc',side_effect=AssertionError('unexpected update')):
            repeat=m.run_import(self.cfg,self.plan)
        self.assertEqual(repeat['status'],'ok',repeat);self.assertEqual(repeat['counts']['unchanged'],1)
        self.assertEqual(len(self.remote.docs),1)

    def test_array_text_rejects_mixed_and_other_arrays_before_writes(self):
        for key in m.ARRAY_TEXT:
            for value in [[1],['ok',None],[{}],[['nested']],True]:
                with self.subTest(key=key,value=value):
                    plan=copy.deepcopy(self.plan);plan['cases'][0][key]=value
                    errors=m.validate_plan(plan,{'cases':m.CASES});self.assertTrue(any(e['path']=='cases[0].'+key for e in errors))
                    result=m.run_import(self.cfg,plan);self.assertEqual(result['status'],'failed');self.assertEqual(self.remote.writes+self.remote.uploads,0)
        plan=copy.deepcopy(self.plan);plan['cases'][0]['用例标题']=['title'];self.assertTrue(m.validate_plan(plan,{'cases':m.CASES}))
        with self.assertRaises(ValueError):m.content_hash(dict(self.plan['cases'][0],需求编号=['REQ',None]))

    def test_empty_array_null_and_existing_numbering(self):
        raw=dict(self.plan['cases'][0],测试步骤=[],预期结果=['already'],需求编号=[],设计方法=[])
        plan=dict(self.plan,cases=[raw]);self.assertFalse(m.validate_plan(plan,{'cases':m.CASES}))
        normalized=m.normalized(raw);self.assertEqual(normalized['测试步骤'],'');self.assertEqual(normalized['预期结果'],'1. already')
        self.assertEqual(normalized['需求编号'],'');self.assertEqual(normalized['设计方法'],'')
        plan['cases']=[dict(raw,预期结果=[])];self.assertTrue(m.validate_plan(plan,{'cases':m.CASES}))
        self.assertEqual(m.normalized(dict(raw,测试步骤=['1. 原文']))['测试步骤'],'1. 1. 原文')
        self.assertEqual(m.content_hash(dict(raw,需求编号=None)),m.content_hash(dict(raw,需求编号='')))
        self.assertEqual(m.normalized(dict(raw,扩展字段={'array':['x','y']}))['扩展字段'],{'array':['x','y']})

    def test_array_document_only_uses_normalized_text(self):
        self.plan['cases']=[self.array_case()];self.plan['targets']['base']=False
        result=m.run_import(self.cfg,self.plan);self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.remote.rows['cases'],[]);self.assertIn('1. 操作一<br>2. 操作二',next(iter(self.remote.docs.values())))
