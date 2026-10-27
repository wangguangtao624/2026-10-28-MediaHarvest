"""Browser-compatible HTTP for short links and streamed media."""
import os
os.environ.setdefault('no_proxy','*')
os.environ.setdefault('NO_PROXY','*')
from curl_cffi import requests

class HttpClient:
    def __init__(self,cookie='',referer='',timeout=20):
        self.timeout=timeout
        headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'}
        if referer:headers['Referer']=referer
        if cookie:headers['Cookie']=cookie
        self.session=requests.Session(impersonate='chrome',headers=headers)
    def get(self,url,params=None,timeout=None,stream=False):
        return self.session.get(url,params=params,timeout=timeout or self.timeout,stream=stream)
