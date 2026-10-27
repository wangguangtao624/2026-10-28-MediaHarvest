"""Folders, atomic JSON settings, and one Markdown file per video. No vault/DB."""
import json,os,re,threading,uuid
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'data/light'
LOCK=threading.RLock()

def read(path,default):
    try:return json.loads(Path(path).read_text('utf-8'))
    except (OSError,ValueError):return default

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8');os.replace(temporary,path)

def safe_name(value,limit=90):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]',' ',str(value));text=re.sub(r'\s+',' ',text).strip(' .')[:limit].rstrip(' .')
    if not text:return '未命名'
    if text.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:text='未命名 '+text
    return text

class Store:
    def __init__(self):STATE.mkdir(parents=True,exist_ok=True)
    def settings(self):
        return {'output':str(ROOT/'逐字稿'),'theme':'新闻','model':'small','workers':2,'downloads':4,'batch_size':16,'device':'auto','roots':[]}|read(STATE/'settings.json',{})
    def save(self,values):
        with LOCK:
            old=self.settings();allowed={k:values[k] for k in ('output','theme','model','workers','downloads','batch_size','device') if k in values};data=old|allowed
            base=Path(data['output']).expanduser()
            if not base.is_absolute():raise ValueError('输出目录需要本机绝对路径')
            data['output']=str(base.resolve());data['theme']=safe_name(data['theme'] or '未分类',50)
            if data['model'] not in ('tiny','base','small','medium','large-v3','large-v3-turbo'):raise ValueError('无效转录模型')
            for k,lo,hi in (('workers',1,4),('downloads',1,8),('batch_size',1,32)):
                data[k]=int(data[k])
                if not lo<=data[k]<=hi:raise ValueError(k+' 超出范围')
            if data['device'] not in ('auto','cuda','cpu'):raise ValueError('无效计算设备')
            folder=base/data['theme'];folder.mkdir(parents=True,exist_ok=True)
            data['roots']=list(dict.fromkeys([data['output']]+old.get('roots',[])))[:10]
            write(STATE/'settings.json',data);return data
    def themes(self,root=None):
        base=Path(root or self.settings()['output'])
        return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith('.')) if base.is_dir() else []
    def document(self,item,transcript,summary,settings,*,note=''):
        base=Path(settings['output']).resolve();folder=(base/safe_name(settings['theme'],50)).resolve()
        if not folder.is_relative_to(base):raise ValueError('主题目录不可指向输出目录以外')
        folder.mkdir(parents=True,exist_ok=True)
        date=str(item.get('publish_time',''))[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):date=datetime.now().strftime('%Y-%m-%d')
        title=safe_name(item.get('title') or '未命名视频')
        with LOCK:
            index=read(STATE/'history.json',[])
            previous=next((x for x in index if x['id']==item['item_id'] and x['folder']==str(folder)),None)
            path=Path(previous['path']) if previous else folder/f'{date} {title}.md'
            if not previous and path.exists():path=folder/f'{date} {title}（{safe_name(item["item_id"])[-6:]}）.md'
            bullets='\n'.join('- '+str(s).strip() for s in summary if str(s).strip()) or '总结生成中，逐字稿原文已完整保存。'
            body=f'# {item.get("title") or title}\n\n主题：{settings["theme"]}\n\n日期：{date}\n\n来源：{item.get("url") or "本地文件"}\n\n## 精简总结\n\n{bullets}\n'
            if note:body+='\n> '+note+'\n'
            body+='\n## 逐字稿原文\n\n'+transcript+'\n'
            temp=path.with_name(path.name+'.tmp');temp.write_text(body,'utf-8');os.replace(temp,path)
            record={'id':item['item_id'],'title':item.get('title') or title,'theme':settings['theme'],'folder':str(folder),'path':str(path),'date':date,'updated':datetime.now().isoformat(timespec='seconds'),'summary_ready':bool(summary)}
            write(STATE/'history.json',[record]+[x for x in index if not (x['id']==record['id'] and x['folder']==record['folder'])][:2000])
            return record
    def history(self):return [r for r in read(STATE/'history.json',[]) if Path(r['path']).is_file()]
