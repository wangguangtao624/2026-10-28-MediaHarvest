"""One pipeline: bounded download → shared batched GPU → SenseNova → Markdown."""
import hashlib,json,os,threading,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from .core.cookie_manager import CookieManager
from .core.http import HttpClient
from .crawler.douyin import Douyin
from .gpu import Engine,hardware
from .store import Store,STATE,ROOT,read,write,safe_name
from .summary import Summarizer

class Service:
    def __init__(self):
        self.store=Store();self.cookies=CookieManager();self.summary=Summarizer();self.engine=Engine()
        self.jobs={};self.controls={};self.lock=threading.RLock();self.background=ThreadPoolExecutor(2,thread_name_prefix='control')
        self.browser_open=False;self.store.save({})
        self.download_pool=ThreadPoolExecutor(8,thread_name_prefix='download')
        self.gpu_pool=ThreadPoolExecutor(4,thread_name_prefix='gpu')
        self.summary_pool=ThreadPoolExecutor(2,thread_name_prefix='summary')
    def settings(self):
        s=self.store.settings()
        return s|{'themes':self.store.themes(),'hardware':hardware(),'api_configured':bool(self.summary.key()),'summary_model':self.summary.config()['model'],'cookie_count':len(self.cookies.active('douyin'))}
    def save_settings(self,data):
        with self.lock:
            if any(j['state'] in ('running','queued','cancelling') for j in self.jobs.values()):raise ValueError('请等当前任务结束后再调整目录或并行参数')
            s=self.store.save(data)
            self.summary.configure(str(data.get('api_key','')).strip(),str(data.get('summary_model','')).strip())
            if str(data.get('cookie','')).strip():self.cookies.save('douyin',data['cookie'])
        return self.settings()
    def job(self,kind,items=None):
        with self.lock:
            jid=uuid.uuid4().hex;job={'id':jid,'kind':kind,'state':'queued','message':'等待中','items':items or [],'warning':'','created':time.time(),'finished':None}
            self.jobs[jid]=job;self.controls[jid]=threading.Event()
            if len(self.jobs)>20:
                for key in list(self.jobs):
                    if len(self.jobs)<=20:break
                    if self.jobs[key]['state'] not in ('queued','running','cancelling'):self.jobs.pop(key);self.controls.pop(key,None)
            return job
    def update(self,job,**fields):
        with self.lock:job.update(fields)
    def item(self,job,index,**fields):
        with self.lock:job['items'][index].update(fields)
    def snapshot(self,jid):
        with self.lock:
            if jid not in self.jobs:raise ValueError('任务记录已过期，请重新读取链接')
            j=json.loads(json.dumps(self.jobs[jid],ensure_ascii=False));items=j['items']
            j['counts']={state:sum(x.get('stage')==state for x in items) for state in ('done','failed','cancelled','downloading','transcribing','summarizing','queued')}
            j['elapsed']=round((j.get('finished') or time.time())-j['created'],1)
            return j
    def discover(self,text,count):
        count=int(count)
        if not 1<=count<=1000:raise ValueError('标题上限须为 1–1000')
        if self.browser_open:raise ValueError('请完成抖音验证并关闭验证浏览器后再读取链接')
        job=self.job('discover');cancel=self.controls[job['id']]
        def run():
            self.update(job,state='running')
            try:
                result=Douyin(self.cookies).discover(text,count,lambda msg:self.update(job,message=msg),cancel.is_set)
                for item in result['items']:item.update(stage='ready',progress=0)
                self.update(job,state='done',message=f'获取 {len(result["items"])} 个标题',items=result['items'],warning=result['warning'],pages=result['pages'])
                write(STATE/'last-collection.json',{'id':job['id'],'items':result['items'],'warning':result['warning'],'pages':result['pages']})
            except Exception as exc:self.update(job,state='cancelled' if cancel.is_set() else 'failed',message=str(exc))
            finally:self.update(job,finished=time.time())
        self.background.submit(run);return {'job_id':job['id']}
    def candidates(self):return read(STATE/'last-collection.json',{'id':'','items':[],'warning':''})
    def add_upload(self,title,path,digest):
        record={'item_id':'local-'+digest[:20],'title':Path(title).stem,'url':'','author':'本地文件','duration':0,'publish_time':'','local_path':str(path),'stream_url':'','media_kind':'video','stage':'ready','progress':0,'selected':True}
        with self.lock:
            current=self.candidates();current['id']=current['id'] or uuid.uuid4().hex
            current['items']=[x for x in current['items'] if x['item_id']!=record['item_id']]+[record];write(STATE/'last-collection.json',current)
        return record
    def start(self,selected):
        with self.lock:
            if any(j['kind']=='process' and j['state'] in ('running','queued','cancelling') for j in self.jobs.values()):raise ValueError('当前批次正在处理，请勿重复提交')
            lookup={i['item_id']:i for i in self.candidates()['items']}
            if not isinstance(selected,list) or not selected or not set(selected)<=lookup.keys():raise ValueError('请勾选本批视频')
            items=[dict(lookup[k],stage='queued',progress=0,error='') for k in dict.fromkeys(selected)]
            settings=self.store.save({});job=self.job('process',items)
        self.background.submit(self._process,job,settings)
        return {'job_id':job['id']}
    def _download(self,item,cancel,progress):
        if cancel():raise InterruptedError('已取消')
        if item.get('local_path') and Path(item['local_path']).is_file():return Path(item['local_path'])
        # Reuse downloads from the previous version without modifying/deleting them.
        for p in (ROOT/'data/workbench/media').glob('*/'+item['item_id']+'.*'):
            if p.is_file() and p.suffix.lower() in ('.mp4','.mp3','.m4a','.wav','.webm'):return p
        directory=STATE/'media';directory.mkdir(exist_ok=True);path=directory/(safe_name(item['item_id'])+'.mp4')
        if path.exists() and path.stat().st_size>1024:return path
        detail=Douyin(self.cookies).detail(item)
        if detail.get('media_kind')=='image':raise ValueError('该链接是图文，没有可转录语音')
        url=detail.get('stream_url')
        if not url:raise ValueError('没有取得视频下载地址，请重新读取链接')
        host=urlsplit(url).hostname or '';cookie=self.cookies.to_header('douyin') if host=='douyin.com' or host.endswith('.douyin.com') else ''
        client=HttpClient(cookie=cookie,referer='https://www.douyin.com/')
        response=client.get(url,timeout=90,stream=True)
        if response.status_code!=200:raise ValueError('视频下载 HTTP '+str(response.status_code))
        total=int(response.headers.get('Content-Length') or 0);got=0;partial=path.with_suffix('.part')
        try:
            with partial.open('wb') as output:
                for chunk in response.iter_content():
                    if cancel():raise InterruptedError('已取消')
                    if not chunk:continue
                    output.write(chunk);got+=len(chunk)
                    progress(min(99,round(got/total*100)) if total else 0)
            if got<1024:raise ValueError('视频下载内容异常，文件过小')
            os.replace(partial,path);return path
        finally:response.close()
    def _process(self,job,settings):
        cancel=self.controls[job['id']];self.update(job,state='running',message='下载、GPU 转录和总结流水线已启动')
        workers=settings['workers'] if settings['device']!='cpu' and hardware()['cuda'] else 1
        slots=threading.BoundedSemaphore(settings['downloads']+workers)
        gpu_slots=threading.BoundedSemaphore(workers);network_slots=threading.BoundedSemaphore(settings['downloads'])
        condition=threading.Condition();remaining=len(job['items'])
        def terminal(index,stage,error=''):
            nonlocal remaining
            self.item(job,index,stage=stage,error=error,progress=100 if stage=='done' else job['items'][index].get('progress',0))
            with condition:remaining-=1;condition.notify_all()
        def summarize(index,record,text):
            try:
                if cancel.is_set():raise InterruptedError('已取消；逐字稿原文已保存')
                summary=self.summary.summarize(text,settings['theme'],cancel.is_set)
                if cancel.is_set():raise InterruptedError('已停止；逐字稿原文已保存')
                saved=self.store.document(record,text,summary,settings);self.item(job,index,file=saved['path']);terminal(index,'done')
            except Exception as exc:
                try:self.store.document(record,text,[],settings,note=str(exc))
                except OSError:pass
                terminal(index,'cancelled' if cancel.is_set() else 'failed',str(exc))
        def transcribe(index,record,path):
            try:
                if cancel.is_set():raise InterruptedError('已取消')
                cache=STATE/'transcripts'/(safe_name(record['item_id'])+'-'+settings['model']+'.json')
                data=read(cache,{})
                self.item(job,index,stage='transcribing',progress=0,asr_cached=bool(data.get('text')))
                if not data.get('text'):
                    with gpu_slots:
                        if cancel.is_set():raise InterruptedError('已取消')
                        data=self.engine.transcribe(path,size=settings['model'],workers=workers,batch_size=settings['batch_size'],device=settings['device'],progress=lambda value:self.item(job,index,progress=value),cancel=cancel.is_set)
                    write(cache,data)
                if cancel.is_set():raise InterruptedError('已取消')
                saved=self.store.document(record,data['text'],[],settings)
                self.item(job,index,stage='summarizing',progress=100,file=saved['path'],device=data['device'],compute=data['compute'],seconds=data['seconds'],duration=data['duration'])
                # Only generated temporary media; user originals/legacy downloads survive.
                if path and path.resolve().is_relative_to(STATE.resolve()) and path.parent.name in ('media','uploads'):path.unlink(missing_ok=True)
                self.summary_pool.submit(summarize,index,record,data['text'])
            except Exception as exc:terminal(index,'cancelled' if cancel.is_set() else 'failed',str(exc))
            finally:slots.release()
        def download(index,record):
            acquired=False
            try:
                while not slots.acquire(timeout=.3):
                    if cancel.is_set():raise InterruptedError('已取消')
                acquired=True;self.item(job,index,stage='downloading',progress=0)
                cached=read(STATE/'transcripts'/(safe_name(record['item_id'])+'-'+settings['model']+'.json'),{})
                with network_slots:
                    path=None if cached.get('text') else self._download(record,cancel.is_set,lambda value:self.item(job,index,progress=value))
                self.gpu_pool.submit(transcribe,index,record,path)
            except Exception as exc:
                if acquired:slots.release()
                terminal(index,'cancelled' if cancel.is_set() else 'failed',str(exc))
        for index,record in enumerate(job['items']):self.download_pool.submit(download,index,record)
        with condition:
            while remaining:condition.wait(1)
        failed=sum(i['stage']=='failed' for i in job['items']);done=sum(i['stage']=='done' for i in job['items'])
        self.update(job,state='cancelled' if cancel.is_set() else 'partial' if failed and done else 'failed' if failed else 'done',message=f'{done} 个文件完成，{failed} 个需重试',finished=time.time())
    def cancel(self,jid):
        if jid not in self.controls:raise ValueError('任务不存在')
        self.controls[jid].set();self.update(self.jobs[jid],state='cancelling',message='正在停止后续步骤；已生成原文保留');return self.snapshot(jid)
    def verify_browser(self,url='https://www.douyin.com/'):
        if self.browser_open:raise ValueError('验证浏览器已打开')
        if any(j['state'] in ('running','queued','cancelling') for j in self.jobs.values()):raise ValueError('请先停止或完成当前任务，再打开验证浏览器')
        self.browser_open=True
        def run():
            try:
                from .crawler.douyin_browser import verify_browser
                verify_browser(self.cookies,url)
            finally:self.browser_open=False
        self.background.submit(run);return {'opened':True}
