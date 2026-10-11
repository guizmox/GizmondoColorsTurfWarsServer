"""HTTP gateway matching /applications/games/colors/open/command.do."""
import argparse
import base64
import contextlib
import hmac
import json
import logging
import os
import signal
import socketserver
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from .service import Service, Rules, GameError, COMMAND_ROOTS, add
from .world import World, Tiles

MAX_BODY=3*1024*1024
ENDPOINT='/applications/games/colors/open/command.do'

class BoundedHTTPServer(socketserver.ThreadingMixIn,HTTPServer):
    daemon_threads=False
    block_on_close=True
    def __init__(self,address,service,tiles,token=''):
        self.service,self.tiles,self.token=service,tiles,token
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            try:request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:self.shutdown_request(request)
            return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def log_message(self,format,*args):
        # No raw URL/body: coordinates and messages must not enter access logs.
        logging.info('HTTP method=%s status=%s',self.command,args[1] if len(args)>1 else '-')
    def reply(self,status,data,content_type='application/xml; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
        self.end_headers();self.wfile.write(data);self.close_connection=True
    def error_xml(self,reason,status=200,code="ColorsXMLException"):
        # f4848 reads the reason node's CDATA value directly, while normal
        # scalar fields use a __text child. Plain reason text loses the type.
        value=('exceptions.'+code+': '+reason).replace(']]>',']]]]><![CDATA[>')
        self.reply(status,('<error><reason><![CDATA['+value+']]></reason></error>').encode('utf-8'))
    def do_GET(self):
        if self.path=='/health':
            self.reply(200,b'{"service":"ColorsRebuilt","status":"ok"}','application/json')
        else:self.reply(404,b'Not found','text/plain')
    def do_POST(self):
        started=time.monotonic();label='unknown'
        if self.path not in (ENDPOINT,'/colors.php'):
            self.reply(404,b'Not found','text/plain');return
        if self.server.token and not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):
            self.error_xml('Access token required',401);return
        try:
            if self.headers.get('Transfer-Encoding'):raise GameError('Chunked uploads unsupported')
            length=int(self.headers.get('Content-Length','-1'))
            if not 0<=length<=MAX_BODY:raise GameError('Invalid body length')
            body=self.rfile.read(length)
            if len(body)!=length:raise GameError('Truncated body')
            kind=self.headers.get('Content-Type','').split(';')[0].strip().lower()
            if kind not in ('application/x-www-form-urlencoded',''):raise GameError('Expected form data')
            form=urllib.parse.parse_qs(body.decode('ascii'),keep_blank_values=True,strict_parsing=True,max_num_fields=4)
            if set(form)!= {'command','data'} or any(len(v)!=1 for v in form.values()):raise GameError('Expected one command and one data field')
            command=form['command'][0]
            label=command if command in COMMAND_ROOTS else 'unknown'
            logging.info('Colors command=%s received',label)
            result=self.server.service.handle(command,form['data'][0])
            if command=='getmapview' and self.server.tiles:
                # Network tile reads occur after the gameplay transaction closes.
                xy=result.find('turflist/turf/coordinates').text
                sx,sy=map(int,xy.split(','));x,y=sx+3,sy+2
                add(result,'image-data',base64.b64encode(self.server.tiles.render(x,y)).decode('ascii'))
            data=ET.tostring(result,encoding='utf-8',xml_declaration=True,short_empty_elements=False)
            if command=='getmapview' and result.find('image-data') is not None:
                # e5814 reads the image node's CDATA storage directly, just
                # like f4848's error reason. Ordinary text leaves it NULL.
                # This value is Base64, which cannot contain a CDATA terminator.
                data=data.replace(b'<image-data>',b'<image-data><![CDATA[',1).replace(b'</image-data>',b']]></image-data>',1)
            self.reply(200,data)
            logging.info('Colors command=%s result=ok duration_ms=%d',label,int((time.monotonic()-started)*1000))
        except GameError as exc:
            logging.info('Colors command=%s result=%s duration_ms=%d',label,exc.code,int((time.monotonic()-started)*1000))
            self.error_xml(str(exc),code=exc.code)
        except (ValueError,ET.ParseError) as exc:
            logging.info('Colors command=%s result=ColorsXMLException duration_ms=%d',label,int((time.monotonic()-started)*1000))
            self.error_xml(str(exc))
        except (OSError,TimeoutError) as exc:
            logging.warning('Map/transport request failed: %s',type(exc).__name__)
            self.error_xml('Map provider unavailable; please retry',code='MapInfoConnectionException')
        except sqlite3.OperationalError:
            logging.warning('Database unavailable or busy')
            self.error_xml('Server busy; please retry',503)
        except Exception:
            logging.exception('Server command failed')
            self.error_xml('Internal server error',500)

def main():
    parser=argparse.ArgumentParser(description='Colors Turf War server reconstruction')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--database',default='data/colors.sqlite3')
    parser.add_argument('--origin-lat',type=float,default=48.8566)
    parser.add_argument('--origin-lon',type=float,default=2.3522)
    parser.add_argument('--tile-url',default='https://tile.openstreetmap.org/{z}/{x}/{y}.png')
    parser.add_argument('--tile-cache',default='data/tiles')
    parser.add_argument('--tile-agent',default='ColorsRebuilt/0.1')
    parser.add_argument('--rules',help='JSON overrides for configurable replacement game rules')
    args=parser.parse_args()
    rules=Rules(**json.loads(Path(args.rules).read_text())) if args.rules else Rules()
    if rules.income_interval<=0 or rules.attack_expiry<=rules.attack_delay:parser.error('Invalid rule timings')
    world=World(args.origin_lat,args.origin_lon)
    service=Service(args.database,rules,world)
    tiles=Tiles(world,args.tile_url,args.tile_cache,args.tile_agent)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    with BoundedHTTPServer((args.host,args.port),service,tiles,os.environ.get('COLORS_ACCESS_TOKEN','')) as server:
        print(f'ColorsRebuilt listening on {args.host}:{args.port}')
        # shutdown must execute outside serve_forever's thread.
        previous = signal.getsignal(signal.SIGTERM)
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)

if __name__=='__main__':main()
