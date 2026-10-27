"""Douyin metadata: paginate by cursor; keep explicit partial results."""
import re
from datetime import datetime
from ..core.http import HttpClient
from ..core.models import Item
from urllib.parse import urlsplit
URL_RE=re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
def clean_url(text):
    found=URL_RE.search(text)
    return found.group().rstrip('.,;:!?)') if found else text.strip()

PROFILE=re.compile(r'/user/(MS4wLjABAAAA[\w-]+)')
VIDEO=re.compile(r'/(?:video|note|slides)/(\d+)')

def item_of(data):
    aid=str(data.get('aweme_id') or '');video=data.get('video') or {};urls=(video.get('play_addr') or {}).get('url_list') or []
    title=str(data.get('desc') or '未命名视频').strip();title=re.split(r'\s*#',title)[0].strip() or title[:90]
    timestamp=data.get('create_time');date=datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d') if isinstance(timestamp,(int,float)) and timestamp>0 else ''
    return Item(aid,title=title[:100],desc=str(data.get('desc') or ''),author=str((data.get('author') or {}).get('nickname') or ''),url='https://www.douyin.com/video/'+aid,duration=(video.get('duration') or data.get('duration') or 0)/1000,publish_time=date,stream_url=str(urls[0]) if urls else '',media_kind='image' if data.get('images') and not video.get('duration') else 'video')

class Douyin:
    def __init__(self,cookies):
        self.cookies=cookies;self.client=HttpClient(referer='https://www.douyin.com/',timeout=20)
    def resolve(self,text):
        value=clean_url(text);host=urlsplit(value).hostname or ''
        if not (host=='douyin.com' or host.endswith('.douyin.com')):raise ValueError('当前仅支持抖音作品或博主主页链接')
        p=PROFILE.search(value);v=VIDEO.search(value)
        if p:return 'creator',p.group(1)
        if v:return 'manual',v.group(1)
        if not re.match(r'https?://(?:www\.|v\.)?douyin\.com/',value):raise ValueError('请粘贴抖音作品、分享短链或博主主页链接')
        response=self.client.get(value);final=str(response.url);p=PROFILE.search(final);v=VIDEO.search(final)
        if p:return 'creator',p.group(1)
        if v:return 'manual',v.group(1)
        raise ValueError('分享链接未能解析到公开作品或主页')
    def discover(self,text,count=100,progress=lambda _:None,cancel=lambda:False):
        links=list(dict.fromkeys(u.rstrip('.,;:!?)') for u in URL_RE.findall(text)))
        if not links:raise ValueError('没有找到链接；可直接粘贴抖音分享文案')
        found={};warnings=[];pages=[]
        for link in links:
            if cancel():raise InterruptedError('任务已取消')
            mode,value=self.resolve(link);progress('正在读取'+('博主作品列表' if mode=='creator' else '视频标题'))
            from .douyin_browser import fetch
            result=fetch(self.cookies,mode,value,count,progress,cancel)
            rows=result['items'];pages.extend(result['pages'])
            if result['warning']:warnings.append(result['warning'])
            for data in rows:
                if not isinstance(data,dict) or not data.get('aweme_id'):continue
                item=item_of(data)
                if mode=='creator' and len([i for i in found.values() if i.get('_creator')==value])>=count:break
                record=item.to_dict();record['_creator']=value if mode=='creator' else '';found.setdefault(item.item_id,record)
        return {'items':list(found.values()),'warning':'\n'.join(dict.fromkeys(warnings)),'pages':pages}
    def detail(self,item):
        if item.get('stream_url'):return item
        from .douyin_browser import fetch
        data=fetch(self.cookies,'manual',item['item_id'],1)['items'][0]
        return item|item_of(data).to_dict()
