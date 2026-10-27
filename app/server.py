"""Small loopback-only HTTP UI for transcript files."""
import hashlib,json,os,subprocess,threading,urllib.parse,uuid,webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from .service import Service
from .store import STATE

WEB=Path(__file__).parent/'web'
class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,port,service):super().__init__(('127.0.0.1',port),Handler);self.service=service

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,value,status=200,mime='application/json; charset=utf-8',filename=''):
        body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(filename))
        try:self.end_headers();self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    def url(self):
        parsed=urllib.parse.urlsplit(self.path);return parsed.path,{k:v[0] for k,v in urllib.parse.parse_qs(parsed.query).items()}
    def do_GET(self):
        try:
            path,q=self.url();p=self.server.service
            if path in ('/','/app.js','/style.css'):
                filename='index.html' if path=='/' else path[1:];mime={'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}[filename]
                return self.send((WEB/filename).read_bytes(),mime=mime+'; charset=utf-8')
            if path=='/api/health':return self.send({'status':'ok','mode':'transcript'})
            if path=='/api/settings':return self.send(p.settings())
            if path=='/api/themes':return self.send(p.store.themes(q.get('root')))
            if path=='/api/candidates':return self.send(p.candidates())
            if path=='/api/history':return self.send(p.store.history())
            if path=='/api/jobs':return self.send([p.snapshot(jid) for jid in list(p.jobs)[-10:]])
            if path.startswith('/api/jobs/'):return self.send(p.snapshot(path.split('/')[-1]))
            if path=='/api/file':
                record=next((r for r in p.store.history() if r['path']==q.get('path')),None)
                if not record:raise ValueError('文件不在当前生成历史中')
                file=Path(record['path'])
                if q.get('download'):return self.send(file.read_bytes(),mime='text/markdown; charset=utf-8',filename=file.name)
                return self.send({'text':file.read_text('utf-8'),'path':str(file)})
            return self.send({'error':'路径不存在'},404)
        except Exception as exc:self.send({'error':str(exc)},400)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin','')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):raise ValueError('请求来源不匹配')
            path,q=self.url();p=self.server.service;size=int(self.headers.get('Content-Length','0'))
            if path=='/api/upload':
                title=q.get('name','audio.wav');suffix=Path(title).suffix.lower()
                if suffix not in ('.mp4','.mov','.mkv','.webm','.mp3','.m4a','.wav','.flac','.aac'):raise ValueError('请选择音视频文件；链接清单请用 .txt 导入')
                if not 0<size<=2_000_000_000:raise ValueError('文件大小须小于 2GB')
                directory=STATE/'uploads';directory.mkdir(exist_ok=True);temp=directory/(uuid.uuid4().hex+suffix);digest=hashlib.sha256()
                try:
                    with temp.open('wb') as output:
                        left=size
                        while left:
                            chunk=self.rfile.read(min(left,1048576))
                            if not chunk:raise ValueError('文件上传中断')
                            digest.update(chunk);output.write(chunk);left-=len(chunk)
                    return self.send(p.add_upload(title,temp,digest.hexdigest()),201)
                except Exception:temp.unlink(missing_ok=True);raise
            if size<0 or size>4_000_000:raise ValueError('请求过大')
            data=json.loads(self.rfile.read(size) or b'{}')
            if path=='/api/settings':return self.send(p.save_settings(data))
            if path=='/api/discover':return self.send(p.discover(data.get('text',''),data.get('count',100)),202)
            if path=='/api/start':return self.send(p.start(data.get('selected_ids')),202)
            if path=='/api/cancel':return self.send(p.cancel(data['job_id']))
            if path=='/api/browser':return self.send(p.verify_browser())
            if path=='/api/choose-folder':
                command="[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Select transcript output folder'; if($d.ShowDialog() -eq 'OK'){ConvertTo-Json -InputObject $d.SelectedPath -Compress}"
                result=subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',command],capture_output=True,timeout=180)
                raw=result.stdout.decode('utf-8','replace').strip()
                return self.send({'path':json.loads(raw) if raw else ''})
            if path=='/api/open-folder':os.startfile(p.store.settings()['output']);return self.send({'opened':True})
            return self.send({'error':'路径不存在'},404)
        except Exception as exc:self.send({'error':str(exc)},400)

def serve(port=8765,open_browser=False):
    service=Service()
    config=service.store.settings()
    try:
        print('Preparing speech model (first launch may download it)...',flush=True)
        service.engine.load(config['model'],config['workers'],config['device'])
        print('ASR ready: '+service.engine.device+' / '+service.engine.compute,flush=True)
    except Exception as exc:print('ASR preparation: '+str(exc),flush=True)
    server=Server(port,service)
    url=f'http://127.0.0.1:{port}';print('MediaHarvest Lite: '+url,flush=True)
    if open_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        for control in service.controls.values():control.set()
        server.server_close();service.background.shutdown(wait=False,cancel_futures=True)
        for pool in (service.download_pool,service.gpu_pool,service.summary_pool):pool.shutdown(wait=False,cancel_futures=True)
