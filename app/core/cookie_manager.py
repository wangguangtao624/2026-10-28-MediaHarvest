"""Local Douyin cookies, compatible with JSON exports and Cookie headers."""
import json,os,time,uuid
from pathlib import Path

class CookieManager:
    def __init__(self,cookie_dir=None):
        self.cookie_dir=Path(cookie_dir or Path(__file__).resolve().parents[2]/'data/cookies');self.cookie_dir.mkdir(parents=True,exist_ok=True)
    def path_of(self,platform='douyin'):return self.cookie_dir/(platform+'.json')
    def exists(self,platform='douyin'):return self.path_of(platform).is_file()
    @staticmethod
    def parse(text):
        raw=(text or '').lstrip('\ufeff').strip()
        if not raw:return []
        if raw.startswith(('[','{')):
            data=json.loads(raw)
            if isinstance(data,dict):data=data.get('cookies',[])
            if not isinstance(data,list):raise ValueError('Cookie JSON 应为浏览器导出的数组')
            return [c for c in data if isinstance(c,dict) and c.get('name') and 'value' in c]
        out=[]
        for part in raw.replace('\n',';').split(';'):
            if '=' not in part:continue
            name,_,value=part.partition('=')
            if name.strip():out.append({'name':name.strip(),'value':value.strip(),'domain':'.douyin.com','path':'/'})
        return out
    def load(self,platform='douyin'):
        try:return self.parse(self.path_of(platform).read_text('utf-8-sig'))
        except (OSError,ValueError):return []
    def save(self,platform,text):
        if platform!='douyin':raise ValueError('当前仅配置抖音 Cookie')
        data=self.parse(text)
        data=[c for c in data if not c.get('domain') or c['domain']=='douyin.com' or c['domain'].endswith('.douyin.com')]
        if not data:raise ValueError('没有解析到抖音 Cookie')
        for c in data:
            c['domain']=c.get('domain') or '.douyin.com';c['path']=c.get('path') or '/'
        path=self.path_of(platform);temp=path.with_name(uuid.uuid4().hex+'.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8');os.replace(temp,path)
        return len(data)
    def active(self,platform='douyin'):
        return [c for c in self.load(platform) if c.get('value') and not (isinstance(c.get('expirationDate'),(int,float)) and 0<c['expirationDate']<=time.time())]
    def to_header(self,platform='douyin'):return '; '.join(c['name']+'='+str(c['value']) for c in self.active(platform))
