"""Replay encrypted HTTP exchanges and execute the ORIGINAL ARM response parsers.

python tools/verify_arm_protocol.py /path/to/Colors.exe [--gif /path/to/map.gif]
The optional GIF can be an actual OSM response; default tiles are offline fixtures.
This verifies protocol components, not a complete interactive game session.
"""
import argparse
import base64
import io
import struct
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('game');parser.add_argument('--gif');args=parser.parse_args()
import arm_runtime as a
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_PC,UC_ARM_REG_LR
from PIL import Image
from colors_server.service import Service,Rules,COMMAND_ROOTS
from colors_server.world import World,Tiles
from colors_server.http import BoundedHTTPServer,ENDPOINT

response_node=0
response_bytes=0
inject_map_response=True
# The original map callback obtains its response through a native HTTP object.
# Here HTTP is real, already completed; inject only the parsed response pointer.
# Skip unread-envelope/global network getters. All XML, turf and crew parsing
# and map traversal below remain original ARM instructions.
def response_hook(uc,address,size,_):
    if address==0xf4848:
        if not inject_map_response:return
        uc.reg_write(UC_ARM_REG_R0,response_node)
    elif address==0xe492c:uc.reg_write(UC_ARM_REG_R0,0)
    uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
for address in (0xf4848,0xe492c,0xe6420):
    a.uc.hook_add(UC_HOOK_CODE,response_hook,begin=address,end=address)

def http_reader_hook(uc,address,size,_):
    result={0xe48e4:0x2060000,0xf3eac:1,0xf3ef4:response_bytes}[address]
    uc.reg_write(UC_ARM_REG_R0,result);uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
for address in (0xe48e4,0xf3eac,0xf3ef4):
    a.uc.hook_add(UC_HOOK_CODE,http_reader_hook,begin=address,end=address)

def decode_gif(raw):
    data=a.malloc(len(raw));a.write(data,raw);result=a.malloc(12)
    a.call(0xa8010,result,data,len(raw))
    width,height,kind,pixels=struct.unpack('<HHII',a.uc.mem_read(result,12))
    assert (width,height)==(315,225) and kind==2 and pixels
    print('PASS: original ARM GIF decoder: 315x225 RGB image')

with tempfile.TemporaryDirectory() as directory:
    rules=Rules(starting_money=500000)
    world=World();service=Service(Path(directory)/'colors.sqlite3',rules,world)
    def fixture_tile(url):
        image=Image.new('RGB',(256,256),'#cfe3dc');buffer=io.BytesIO();image.save(buffer,'PNG');return buffer.getvalue()
    tiles=Tiles(world,'https://fixture.invalid/{z}/{x}/{y}.png',Path(directory)/'tiles',fetch=fixture_tile)
    server=BoundedHTTPServer(('127.0.0.1',0),service,tiles)
    worker=threading.Thread(target=server.serve_forever);worker.start()
    try:
        def request(command,terminal,body='',expected_error=False):
            root=COMMAND_ROOTS[command]
            plain=f'<{root}><terminalhardwareid>{terminal}</terminalhardwareid>{body}</{root}>'.encode()+b'\0'
            pointer=a.malloc(len(plain));a.write(pointer,plain)
            # Execute the game's encryption routine, not the Python encoder.
            a.call(0xe9a08,pointer,len(plain))
            encoded=a.cstr(a.call(0xf4d24,pointer,len(plain))).decode()
            assert encoded==base64.b64encode(a.uc.mem_read(pointer,len(plain))).decode()
            unescaped=f'command={command}&data=<c>{encoded}</c>'.encode()
            buffer=a.malloc(len(unescaped)+1);a.write(buffer,unescaped+b'\0')
            form=a.cstr(a.call(0xf4b60,0,buffer))
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+ENDPOINT,form,
                                      {'Content-Type':'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req,timeout=15) as response:raw=response.read()
            root=ET.fromstring(raw);assert (root.tag=='error')==expected_error,raw
            return root,raw
        for terminal,x in [('Player-A',0),('Player-B',1)]:
            request('getgamevars',terminal)
            error,raw=request('getuserinfo',terminal,expected_error=True)
            assert 'exceptions.NoSuchUserException:' in error.findtext('reason')
            response_bytes=a.malloc(len(raw)+1);a.write(response_bytes,raw+b'\0')
            inject_map_response=False
            conn=a.malloc(0x200);assert a.call(0xf4848,conn)==0
            error_name_pointer=struct.unpack('<I',a.uc.mem_read(conn+0x110,4))[0]
            assert a.cstr(error_name_pointer)==b'NoSuchUserException',(a.cstr(error_name_pointer),hex(error_name_pointer),bytes(a.uc.mem_read(conn+0x104,16)).hex())
            inject_map_response=True
            request('setuserinfo',terminal,f'<colorsname>{terminal}</colorsname><money>{rules.starting_money}</money>')
            latitude,longitude=world.latlon(x*100+50,50)
            request('getmapview',terminal,f'<xpos>{longitude}</xpos><ypos>{latitude}</ypos><map>1</map><metainfo>1</metainfo>')
            created,raw=request('creategang',terminal,f'<turf>{x},0</turf><turfname>Base {terminal}</turfname><gang><color>65536,0,32768</color><name>{terminal} crew</name><tag>HELLO</tag><password>secret</password></gang>')
            assert int(created.findtext('user/money'))==rules.starting_money-rules.turf_price
            user,raw=request('getuserinfo',terminal)
            node=a.parse(raw);obj=a.malloc(128);assert a.call(0xf1ea0,obj,node,1,0)==1
            assert struct.unpack('<I',a.uc.mem_read(obj+12,4))[0]==int(user.findtext('money'))
        print('PASS: native first-login exception parser, two identities, ARM encrypted POST, first-turf purchase and user parser')
        variables,raw=request('getgamevars','Player-A')
        node=a.parse(raw);obj=a.malloc(128);field=a.malloc(4)
        for name,expected,address in [('server.time',int(service.clock())-63072000,0xe5528),
            ('turf.income.waitingtime',rules.income_interval,0xe5528),
            ('turf.timestamp.commence.delay',rules.attack_delay*1000,0xe53d0),
            ('homeboy.price.type1',2000,0xe53d0),('homeboy.price.type2',5000,0xe53d0),('homeboy.price.type3',7000,0xe53d0)]:
            string=a.malloc(len(name)+1);a.write(string,name.encode()+b'\0')
            assert a.call(address,obj,node,string,field)==1,name
            actual=struct.unpack('<I',a.uc.mem_read(field,4))[0]
            assert abs(actual-expected)<=1 if name=='server.time' else actual==expected,(name,actual,expected)
        # Execute the game's countdown unit conversion (db174..db190).
        a.write(0x2070000+0x100c,struct.pack('<I',rules.attack_delay*1000))
        a.uc.emu_start(0xdb174,0xdb190,count=50)
        assert a.uc.reg_read(UC_ARM_REG_R1)==rules.attack_delay
        print('PASS: native game-variable parsers and countdown units (120000ms -> 120s)')
        # Exercise Colors' actual GPS freshness/accuracy guard, f21d8. Only
        # its game-clock getter is adapted to the decoded server clock above.
        gps_clock=int(service.clock())-63072000
        def gps_clock_hook(uc,address,size,_):
            uc.reg_write(UC_ARM_REG_R0,gps_clock)
            uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        clock_hook=a.uc.hook_add(UC_HOOK_CODE,gps_clock_hook,begin=0xe5234,end=0xe5234)
        gps_object=a.malloc(256)
        for age,validated,error,expected in [(0,1,500,1),(301,1,500,0),(0,0,500,0),(0,1,10000,0)]:
            packet=bytearray(180)
            for offset,value in [(1,1),(11,gps_clock-age),(15,validated),(37,error)]:
                struct.pack_into('<I',packet,offset,value)
            a.write(gps_object+0x20,bytes(packet))
            assert a.call(0xf21d8,gps_object)==expected,(age,validated,error)
        gps_clock+=63072000
        struct.pack_into('<I',packet,37,500);a.write(gps_object+0x20,bytes(packet))
        assert a.call(0xf21d8,gps_object)==0,'Unix wire time must fail with a GPS1972 packet'
        a.uc.hook_del(clock_hook)
        print('PASS: original ARM GPS guard accepts corrected wire clock; rejects Unix epoch, stale/invalid/imprecise fixes')
        for terminal,x in [('Player-A',0),('Player-B',1)]:
            latitude,longitude=world.latlon(x*100+50,50)
            metadata,raw=request('getmapview',terminal,f'<xpos>{longitude}</xpos><ypos>{latitude}</ypos><map>1</map><metainfo>1</metainfo>')
            response_node=a.parse(raw);obj=a.malloc(0x3000)
            image_name=a.malloc(8);image_text=a.malloc(11);a.write(image_text,b'image-data\0')
            a.call(0xadf9c,image_name,image_text)
            image_node=a.call(0xb3268,response_node,image_name)
            image_storage=struct.unpack('<I',a.uc.mem_read(image_node+0x34,4))[0]
            assert image_storage,'Map image requires CDATA storage; ordinary XML text crashes e5818'
            image_length,image_pointer=struct.unpack('<II',a.uc.mem_read(image_storage+8,8))
            assert image_length and image_pointer
            assert base64.b64decode(a.uc.mem_read(image_pointer,image_length))==base64.b64decode(metadata.findtext('image-data'))
            # The metadata callback normally decodes then creates two GPU
            # textures. Skip GPU creation only; GIF decoding is tested below.
            a.write(obj+0x840,struct.pack('<II',1,1));a.call(0xe579c,obj)
            assert struct.unpack('<II',a.uc.mem_read(obj+4,8))==(0,0)
            assert struct.unpack('<I',a.uc.mem_read(obj+0x850,4))[0]==2
            turf=a.call(0xe5170,obj,3,2)
            assert struct.unpack('<ii',a.uc.mem_read(turf+0x20,8))==(x,0)
            decode_gif(base64.b64decode(metadata.findtext('image-data')))
        print('PASS: two original ARM map callbacks, 35 territories, centre coordinates and both crews')
        if args.gif:decode_gif(Path(args.gif).read_bytes())
    finally:
        server.shutdown();worker.join();server.server_close()
