"""Replay the supplied retail ARM wallet code against the real HTTP server.

python tools/verify_separate_wallets.py /path/to/autorun.exe --patch-dump /path/to/wallet-patch.bin
The dump is emitted by pocket-core's supplied_retail_image_matches_and_exports_validation_patch
Rust test with POCKETHLE_COLORS_EXE and POCKETHLE_COLORS_PATCH_DUMP set (include ignored tests).
No game binary is distributed. This is an isolated original-ARM test, not a full UI/playthrough.
"""
import argparse
import contextlib
import re
import struct
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('game');parser.add_argument('--patch-dump',required=True);parser.add_argument('--save-zip')
args=parser.parse_args()
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import retail_wallet_runtime as a
from colors_server.codec import encode_request,transform
from colors_server.service import Service,Rules
from colors_server.http import BoundedHTTPServer,ENDPOINT
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_PC,UC_ARM_REG_LR

def integer(address):return struct.unpack('<I',a.uc.mem_read(address,4))[0]
def native_string(data):
    raw=a.malloc(len(data)+1);a.write(raw,data+b'\0');string=a.malloc(8);a.call(0xadbd4,string,raw);return string

def local_balance(money,baseline):
    # Small save with unrelated progression fields; native code performs all
    # encrypted file reads, parsing and writes in a memory-backed CRT filesystem.
    plain=f'Save\n{{\n Character\n {{\n Money {money}\n ServerMoney {baseline}\n Health 280\n }}\n Mission\n {{\n Tutorial\n {{\n Status 4\n }}\n }}\n}}\n'
    a.files['savefile.isv']=transform(plain.encode())

def balances():
    plain=transform(a.files['savefile.isv'],decrypt=True).decode()
    assert re.search(r'Health\s+280',plain) and re.search(r'Status\s+4',plain)
    m=int(re.search(r'\bMoney\s+(\d+)',plain)[1]);b=int(re.search(r'\bServerMoney\s+(\d+)',plain)[1])
    assert a.call(0x17cd8,0,0,1)==m
    return m,b

# Only global/UI/network/config boundaries are adapted. No money arithmetic,
# user XML parser, save codec, save integer reader/writer or request formatter
# is replaced by a host implementation.
identity=native_string(b'ARM-wallet-test');extra=a.malloc(1);a.write(extra,b'\0')
network=a.malloc(512);controller=a.malloc(0x2000);captured=[]
def boundary(uc,address,size,_):
    if address==0x18354:value=1 # this scenario has TurfWarUnlocked=1
    elif address==0xe4534:value=network
    elif address==0xe47c8:value=0
    elif address==0xe4e20:value=identity
    elif address==0xe49d0:value=extra
    elif address==0xf3fe4:
        command=a.cstr(uc.reg_read(UC_ARM_REG_R1)).decode()
        string=uc.reg_read(UC_ARM_REG_R2);captured.append((command,a.cstr(integer(string+4))));value=0
    else:raise AssertionError(hex(address))
    uc.reg_write(UC_ARM_REG_R0,value);uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
for address in [0x18354,0xe4534,0xe47c8,0xe4e20,0xe49d0,0xf3fe4]:
    a.uc.hook_add(UC_HOOK_CODE,boundary,begin=address,end=address)

def receive(root,mode):
    node=root if root.tag=='user' else root.find('user');assert node is not None
    parsed=a.parse(ET.tostring(node));flag=a.malloc(1)
    assert a.call(0xf1a9c,controller+0xfa4,parsed,mode,flag)==1
    return integer(controller+0xfa4+12),a.uc.mem_read(flag,1)[0]

def build_registration():
    name=a.malloc(32);a.write(name,b'ARM wallet test\0');a.call(0xe6880,controller,name)
    return captured[-1]
def build_sync(amount):
    a.call(0xe66d4,controller,amount);return captured[-1]

# Control: demonstrate why a server-only credit contaminates the solo wallet.
local_balance(525,525)
receive(ET.fromstring('<user><colorsname>Test</colorsname><gangid>0</gangid><terminalhardwareid>ARM-wallet-test</terminalhardwareid><money>75000</money></user>'),0)
assert balances()==(75000,75000)
print('PASS control: stock ARM client copies server 75000 into solo Money/ServerMoney')

# Apply EXACT bytes emitted from the production Rust compatibility module.
dump=Path(args.patch_dump).read_bytes();assert len(dump)==136
stub=0x1288a8;a.write(stub,dump[:96]);a.uc.ctl_remove_cache(stub,stub+96)
for at in range(96,len(dump),8):
    address,word=struct.unpack_from('<II',dump,at)
    assert address in [0xf1c14,0xf1c34,0xf1c44,0xf1c7c,0xf1c90]
    a.write(address,struct.pack('<I',word));a.uc.ctl_remove_cache(address,address+4)

with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/'world.db';service=Service(path,Rules(starting_money=75000))
    server=BoundedHTTPServer(('127.0.0.1',0),service,None)
    worker=threading.Thread(target=server.serve_forever);worker.start()
    try:
        def request(command,plain):
            body=urllib.parse.urlencode({'command':command,'data':encode_request(plain)}).encode()
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+ENDPOINT,body,{'Content-Type':'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req,timeout=10) as response:
                assert response.status==200;root=ET.fromstring(response.read())
            assert root.tag!='error',ET.tostring(root);return root
        def simple(command,body=''):
            return request(command,f'<{command}><terminalhardwareid>ARM-wallet-test</terminalhardwareid>{body}</{command}>'.encode())
        local_balance(525,0)
        command,xml=build_registration();assert ET.fromstring(xml).findtext('money')=='525'
        reply=request(command,xml);assert reply.findtext('user/money')=='75000'
        assert receive(reply,2)[0]==75000 and balances()==(525,525)
        print('PASS registration: original client sends 525, server returns 75000, solo remains 525')
        assert receive(simple('getuserinfo'),0)==(75000,0) and balances()==(525,525)
        print('PASS reconnect: no 525 overwrite, no new money sync required')
        view=simple('getmapview','<xpos>2.2945</xpos><ypos>48.8584</ypos><map>1</map><metainfo>1</metainfo>')
        cell=view.findall('turflist/turf')[17].findtext('coordinates')
        reply=simple('buyturf',f'<turf>{cell}</turf><name>ARM home</name>')
        assert receive(reply,1)[0]==45000 and balances()==(525,525)
        print('PASS buy territory: server 45000, solo remains 525')
        local_balance(1525,525) # campaign earned 1000 since last checkpoint
        amount,needs_sync=receive(simple('getuserinfo'),0)
        assert (amount,needs_sync)==(46000,1) and balances()==(1525,1525)
        command,xml=build_sync(amount);assert ET.fromstring(xml).findtext('money')=='46000'
        assert receive(request(command,xml),2)[0]==46000 and balances()==(1525,1525)
        assert receive(simple('getuserinfo'),0)==(46000,0)
        print('PASS solo gain: exactly +1000 online, solo 1525, reconnect does not credit twice')
        local_balance(1025,1525) # solo spending must not debit Turf Wars
        assert receive(simple('getuserinfo'),0)==(46000,0) and balances()==(1025,1025)
        local_balance(1225,1025)
        amount,flag=receive(simple('getuserinfo'),0);assert (amount,flag)==(46200,1)
        assert receive(request(*build_sync(amount)),2)[0]==46200
        print('PASS solo spending/gain: no online debit, following +200 is transferred once')
        # The original forced-first-refresh path must not copy server cash either.
        local_balance(525,0);a.write(0x2071010,b'\x01')
        forced=ET.fromstring('<user><colorsname>Test</colorsname><gangid>0</gangid><terminalhardwareid>ARM-wallet-test</terminalhardwareid><money>75000</money></user>')
        assert receive(forced,0)==(75000,0) and balances()==(525,525)
        assert a.uc.mem_read(0x2071010,1)==b'\0'
        print('PASS forced first refresh: solo 525 retained, baseline checkpointed without injecting bonus')
        # Native signed-overflow path is saturated rather than creating a debit.
        local_balance(2147483647,0)
        fake=ET.fromstring('<user><colorsname>Test</colorsname><gangid>0</gangid><terminalhardwareid>ARM-wallet-test</terminalhardwareid><money>100</money></user>')
        assert receive(fake,0)==(2147483647,1) and balances()==(2147483647,2147483647)
        print('PASS native arithmetic edge: large credit saturates at INT_MAX')
        # Restarting / changing registration rules does not refill an old account.
        reopened=Service(path,Rules(starting_money=150000))
        with contextlib.closing(reopened.connection()) as db:
            assert db.execute('SELECT money FROM users WHERE terminal=?',('ARM-wallet-test',)).fetchone()[0]==46200
        print('PASS persistent existing account: rule change does not top up spent funds')
        if args.save_zip:
            import zipfile
            raw=zipfile.ZipFile(args.save_zip).read('save/savefile.isv')
            original=transform(raw,decrypt=True).split()
            a.files['savefile.isv']=raw
            money=a.call(0x17cd8,0,0,1)
            # No silent retroactive removal of a prior locally granted balance.
            receive(fake,0)
            assert a.call(0x17cd8,0,0,1)==money
            assert transform(a.files['savefile.isv'],decrypt=True).split()==original
            print(f'PASS supplied save: existing solo balance {money} preserved, never silently reset')
    finally:
        server.shutdown();worker.join();server.server_close()
