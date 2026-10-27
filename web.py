"""Launch the lightweight transcript app."""
import argparse,faulthandler
faulthandler.enable()
from app.server import serve
parser=argparse.ArgumentParser()
parser.add_argument('--port',type=int,default=8765)
parser.add_argument('--open',action='store_true')
args=parser.parse_args()
serve(port=args.port,open_browser=args.open)
