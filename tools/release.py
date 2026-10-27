"""Build a source-only lightweight release; exclude credentials and runtime data."""
import hashlib,sys,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.secrets import Secrets
from app.core.cookie_manager import CookieManager

def main():
    names=['README.md','验证说明.md','web.py','启动.bat','run_local_platform.bat','setup.ps1','requirements.txt','requirements-gpu.txt','requirements.lock.txt','.gitignore']
    files=[ROOT/n for n in names]
    files += [p for folder in ('app','tests') for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files.append(Path(__file__))
    secrets=[Secrets(ROOT/'data/light/credentials').get('sensenova')]
    secrets += [str(c['value']) for c in CookieManager().load() if c['name'] in ('sessionid','sessionid_ss','ttwid','sid_tt')]
    for path in files:
        content=path.read_bytes()
        if any(value and value.encode() in content for value in secrets):raise RuntimeError('Private credential found in '+path.name)
    target=ROOT.parent/'MediaHarvest-Lite-20261028.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as package:
        for path in files:package.write(path,Path('MediaHarvest-Lite')/path.relative_to(ROOT))
    with zipfile.ZipFile(target) as package:
        assert package.testzip() is None
        assert not any('/data/' in n or '/.venv/' in n for n in package.namelist())
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.sha256').write_text(digest+'  '+target.name+'\n','utf-8')
    print(f'{target.name}: {target.stat().st_size:,} bytes, {len(files)} files; credential scan and ZIP integrity passed')

if __name__=='__main__':main()
