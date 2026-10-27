import json,re,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app.store import Store,safe_name
from app.core.cookie_manager import CookieManager
from app.crawler.douyin_browser import accepts_response
from app.crawler.douyin import item_of

class TranscriptFiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.state=patch('app.store.STATE',self.root/'state');self.state.start();self.addCleanup(self.state.stop)
        self.store=Store();self.cfg=self.store.save({'output':str(self.root/'output'),'theme':'新闻'})
        self.item={'item_id':'123','title':'今天的新闻','url':'https://www.douyin.com/video/123','publish_time':'2026-10-27'}
    def test_date_title_summary_and_untouched_original(self):
        raw='第一行原文。\n第二行原文包含 **符号**。'
        r=self.store.document(self.item,raw,['精简要点'],self.cfg);p=Path(r['path']);text=p.read_text('utf-8')
        self.assertEqual(p.name,'2026-10-27 今天的新闻.md');self.assertEqual(p.parent.name,'新闻')
        self.assertIn('## 精简总结\n\n- 精简要点',text);self.assertEqual(text.split('## 逐字稿原文\n\n')[1],raw+'\n')
    def test_summary_update_keeps_one_file(self):
        a=self.store.document(self.item,'原文',[],self.cfg);b=self.store.document(self.item,'原文',['总结'],self.cfg)
        self.assertEqual(a['path'],b['path']);self.assertEqual(len(list(Path(a['folder']).glob('*.md'))),1);self.assertTrue(self.store.history()[0]['summary_ready'])
    def test_same_title_different_video_does_not_overwrite(self):
        a=self.store.document(self.item,'甲',['甲'],self.cfg);b=self.store.document(self.item|{'item_id':'456'},'乙',['乙'],self.cfg)
        self.assertNotEqual(a['path'],b['path']);self.assertIn('甲',Path(a['path']).read_text('utf-8'))
    def test_existing_themes_are_reused(self):
        self.store.save({'theme':'科技'});self.store.save({'theme':'新闻'})
        self.assertEqual(self.store.themes(),['新闻','科技']);self.assertEqual(len(list(Path(self.cfg['output']).iterdir())),2)
    def test_relative_output_rejected(self):
        with self.assertRaises(ValueError):self.store.save({'output':'relative/path'})
    def test_windows_names_and_traversal_characters(self):
        self.assertEqual(safe_name('..'),'未命名');self.assertNotIn('/',safe_name('../../新闻'))
        self.assertNotEqual(safe_name('CON.txt'),'CON.txt');self.assertNotIn(':',safe_name('新闻:标题?'))
    def test_failed_summary_still_preserves_original(self):
        r=self.store.document(self.item,'完整逐字稿',[],self.cfg,note='API 429，可重试')
        text=Path(r['path']).read_text('utf-8');self.assertIn('完整逐字稿',text);self.assertIn('API 429',text)

class CollectionRegression(unittest.TestCase):
    def test_pagination_subdomain_is_not_silently_discarded(self):
        for host in ('www.douyin.com','www-hj.douyin.com'):
            self.assertTrue(accepts_response('https://'+host+'/aweme/v1/web/aweme/post/?max_cursor=123','aweme/post/'))
        self.assertFalse(accepts_response('https://douyin.com.attacker.test/aweme/v1/web/aweme/post/','aweme/post/'))
    def test_expired_cookie_is_not_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm=CookieManager(tmp);cm.save('douyin','\ufeff'+json.dumps([{'name':'old','value':'expired','expirationDate':1},{'name':'sessionid','value':'valid'}]))
            self.assertEqual(cm.to_header('douyin'),'sessionid=valid')
    def test_video_metadata_preserves_real_publication_date(self):
        item=item_of({'aweme_id':'123','desc':'新闻标题 #话题','create_time':1793116800,'video':{'duration':60000,'play_addr':{'url_list':['https://media.test/video']}}})
        self.assertEqual(item.title,'新闻标题');self.assertEqual(item.duration,60);self.assertRegex(item.publish_time,r'^2026-10-\d{2}$')

if __name__=='__main__':unittest.main()
