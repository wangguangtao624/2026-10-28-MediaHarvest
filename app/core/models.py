"""Only the video metadata needed by the transcript workflow."""
from dataclasses import dataclass,asdict

@dataclass
class Item:
    item_id:str
    title:str=''
    url:str=''
    author:str=''
    duration:float=0
    publish_time:str=''
    desc:str=''
    stream_url:str=''
    media_kind:str='video'
    error:str=''
    selected:bool=True
    local_path:str=''
    def to_dict(self):return asdict(self)
