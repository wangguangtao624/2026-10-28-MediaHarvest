"""Normal persistent browser pagination. Captchas stop; no bypass/solver."""
import os,threading,time
from pathlib import Path
from urllib.parse import quote,urlsplit,parse_qs
from ..store import STATE,read,write

LOCK=threading.Lock()

def executable():
    for root in (os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')):
        for name in ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'):
            path=Path(root)/name
            if path.is_file():return str(path)
    return None

def context_for(pw,cookies,visible=False):
    options={'headless':not visible,'viewport':{'width':1440,'height':1000}}
    browser=executable()
    if browser:options['executable_path']=browser
    context=pw.chromium.launch_persistent_context(str(STATE/'browser'),**options)
    stamp=cookies.path_of('douyin').stat().st_mtime if cookies.exists('douyin') else 0
    if read(STATE/'browser-import.json',{}).get('stamp')!=stamp:
        context.clear_cookies();values=[]
        for c in cookies.active('douyin'):
            item={'name':c['name'],'value':str(c['value']),'domain':c.get('domain') or '.douyin.com','path':c.get('path') or '/','secure':bool(c.get('secure')),'httpOnly':bool(c.get('httpOnly'))}
            if c.get('expirationDate',0)>0:item['expires']=float(c['expirationDate'])
            values.append(item)
        if values:context.add_cookies(values)
    return context

def save_cookies(context,cookies):
    import json
    values=[]
    try:
        for c in context.cookies():
            if not (c['domain']=='douyin.com' or c['domain'].endswith('.douyin.com')):continue
            c=dict(c);c['expirationDate']=c.pop('expires',-1);values.append(c)
        if values:cookies.save('douyin',json.dumps(values))
        stamp=cookies.path_of('douyin').stat().st_mtime if cookies.exists('douyin') else 0
        write(STATE/'browser-import.json',{'stamp':stamp})
    except Exception:pass

def scroll(page):
    # Douyin listens to real wheel events on its nested route container.
    # Assigning scrollTop skips its page-loading handler; large jumps miss sentinels.
    page.mouse.move(900,700)
    page.mouse.wheel(0,550)


def accepts_response(url,endpoint):
    parsed=urlsplit(url)
    return (parsed.hostname=='douyin.com' or (parsed.hostname or '').endswith('.douyin.com')) and parsed.path.endswith('/'+endpoint)

def fetch(cookies,mode,value,count=100,progress=lambda _:None,cancel=lambda:False):
    from playwright.sync_api import sync_playwright,TimeoutError as BrowserTimeout
    endpoint='aweme/detail/' if mode=='manual' else 'aweme/post/'
    url='https://www.douyin.com/'+('video/' if mode=='manual' else 'user/')+quote(value,safe='')
    items={};pages=[];blocked=False;last_more=None;last_added=time.monotonic();last_received=last_added;warning=''
    with LOCK,sync_playwright() as pw:
        context=context_for(pw,cookies);page=context.pages[0] if context.pages else context.new_page()
        def receive(response):
            nonlocal blocked,last_more,last_added,last_received
            parsed=urlsplit(response.url)
            if not accepts_response(response.url,endpoint):return
            query=parse_qs(parsed.query)
            if mode=='creator' and query.get('sec_user_id',[''])[0]!=value:return
            try:data=response.json()
            except Exception:return
            last_received=time.monotonic()
            if data.get('verify_check') or data.get('status_code') not in (0,None):blocked=True;return
            rows=[data.get('aweme_detail') or {}] if mode=='manual' else data.get('aweme_list') or []
            added=0
            for row in rows:
                aid=str(row.get('aweme_id') or '')
                if not aid or (mode=='manual' and aid!=value):continue
                if mode=='creator' and (row.get('author') or {}).get('sec_uid') not in (None,'',value):continue
                if aid not in items:items[aid]=row;added+=1
            if added:last_added=time.monotonic()
            last_more=data.get('has_more')
            pages.append({'path':'browser','cursor':query.get('max_cursor',['0'])[0],'next_cursor':str(data.get('max_cursor','')),'received':len(rows),'new':added,'has_more':last_more})
        page.on('response',receive)
        try:
            try:page.goto(url,wait_until='domcontentloaded',timeout=30000)
            except BrowserTimeout:pass
            deadline=time.monotonic()+min(300,40+count*1.2);steps=0
            while time.monotonic()<deadline:
                if cancel():raise InterruptedError('任务已取消')
                page.wait_for_timeout(500)
                if blocked or '验证码' in page.title() or '/verify' in page.url:
                    warning='抖音要求验证码。请点击“打开抖音验证”，完成验证并关闭窗口后重试。';break
                progress(f'浏览器已读取 {len(items)} / {count} 条（{len(pages)} 次分页响应）')
                if len(items)>=count:break
                if mode=='manual' and items:break
                scroll(page);steps+=1
                idle=time.monotonic()-max(last_added,last_received)
                # A pinned/initial response with has_more=0 alone is not the end.
                if items and idle>10 and steps>=6:
                    if last_more not in (0,False):warning=f'平台分页暂时停止，已保留 {len(items)} 条；请求上限 {count} 条。'
                    break
            if len(items)<count and last_more not in (0,False) and not warning:warning=f'达到等待上限，已保留 {len(items)} 条，未将其当作博主全部作品。'
            if not items:raise RuntimeError(warning or '抖音网页没有返回作品；请在验证浏览器中确认该主页公开可见')
            return {'items':list(items.values())[:count],'pages':pages,'warning':warning}
        finally:save_cookies(context,cookies);context.close()

def verify_browser(cookies,url='https://www.douyin.com/'):
    from playwright.sync_api import sync_playwright
    with LOCK,sync_playwright() as pw:
        context=context_for(pw,cookies,True);page=context.pages[0] if context.pages else context.new_page()
        page.goto(url,wait_until='domcontentloaded',timeout=30000)
        try:
            deadline=time.monotonic()+900
            while context.pages and time.monotonic()<deadline:
                values=context.cookies();save_cookies(context,cookies)
                try:page.wait_for_timeout(1000)
                except Exception:break
        finally:
            try:save_cookies(context,cookies);context.close()
            except Exception:pass
