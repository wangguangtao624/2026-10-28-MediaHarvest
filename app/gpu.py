"""Persistent FP16 model + batched chunks + concurrent CUDA workers."""
import gc,os,site,subprocess,threading,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
os.environ.setdefault('HF_HOME',str(ROOT/'.cache/huggingface'))
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING','1')
_dll_handles=[]
for directory in site.getsitepackages():
    for name in ('cublas','cudnn','cuda_nvrtc','cuda_runtime'):
        path=Path(directory)/'nvidia'/name/'bin'
        if os.name=='nt' and path.is_dir():
            _dll_handles.append(os.add_dll_directory(str(path)))
            os.environ['PATH']=str(path)+os.pathsep+os.environ.get('PATH','')

def hardware():
    result={'cuda':False,'name':'CPU','memory_mb':0,'reason':''}
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count()<1:raise RuntimeError('未检测到 NVIDIA CUDA 设备')
        if os.name=='nt':
            import ctypes
            for dll in ('cublas64_12.dll','cudnn64_9.dll'):ctypes.WinDLL(dll)
        info=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5)
        name,_,memory=info.stdout.splitlines()[0].rpartition(',')
        result.update(cuda=True,name=name.strip(),memory_mb=int(memory.strip()))
    except Exception as exc:result['reason']=str(exc)[:240]
    return result

class Engine:
    def __init__(self):self.model=None;self.key=None;self.lock=threading.Lock();self.device='';self.compute=''
    def load(self,size='small',workers=2,device='auto'):
        from faster_whisper import WhisperModel
        key=(size,workers,device)
        with self.lock:
            if self.model is not None and self.key==key:return
            self.model=None;gc.collect()
            available=hardware()
            self.device='cuda' if device!='cpu' and available['cuda'] else 'cpu'
            if device=='cuda' and not available['cuda']:raise RuntimeError('GPU 不可用：'+available['reason'])
            self.compute='float16' if self.device=='cuda' else 'int8'
            cache=Path(os.environ['HF_HOME'])/'hub'/('models--Systran--faster-whisper-'+size)
            snapshots=[p for p in (cache/'snapshots').glob('*') if (p/'model.bin').is_file()]
            if snapshots:local=str(max(snapshots,key=lambda p:p.stat().st_mtime))
            else:
                from huggingface_hub import snapshot_download
                local=snapshot_download('Systran/faster-whisper-'+size,allow_patterns=['*.bin','*.json','*.txt'])
            self.model=WhisperModel(local,device=self.device,compute_type=self.compute,num_workers=workers if self.device=='cuda' else 1,cpu_threads=4)
            self.key=key
    def transcribe(self,path,*,size='small',workers=2,batch_size=16,device='auto',progress=lambda _:None,cancel=lambda:False):
        from faster_whisper import BatchedInferencePipeline
        self.load(size,workers,device)
        started=time.perf_counter()
        # Per-call wrapper: timestamps/VAD state are not shared between videos.
        pipeline=BatchedInferencePipeline(self.model)
        segments,info=pipeline.transcribe(str(path),language='zh',batch_size=batch_size if self.device=='cuda' else min(batch_size,4),beam_size=1,best_of=1,condition_on_previous_text=False,vad_filter=True,initial_prompt='以下是简体中文新闻和讲解的逐字稿。')
        text=[]
        for segment in segments:
            if cancel():raise InterruptedError('任务已取消')
            if segment.text.strip():text.append(segment.text.strip())
            progress(min(100,round(segment.end/max(info.duration,1)*100)))
        body='\n'.join(text)
        if not body:raise ValueError('没有识别到人声，原文件已保留')
        return {'text':body,'duration':info.duration,'seconds':round(time.perf_counter()-started,2),'device':self.device,'compute':self.compute,'model':size,'batch_size':batch_size}
