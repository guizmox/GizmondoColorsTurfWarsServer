import base64
import concurrent.futures
import contextlib
import io
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image
from colors_server.codec import encode_request, decode_request, transform
from colors_server.service import Service, Rules, GameError, COMMAND_ROOTS, wire_time, GPS_UTC_EPOCH_OFFSET
from colors_server.world import World, Tiles
from colors_server.http import BoundedHTTPServer, ENDPOINT

class ServiceTests(unittest.TestCase):
    def test_absolute_wire_dates_match_gps_epoch_and_keep_duration_units(self):
        self.assertEqual(wire_time(0),0)
        variables=self.request('getgamevars')
        values={v.findtext('name'):int(v.findtext('value')) for v in variables.findall('var')}
        self.assertEqual(values['server.time'],int((self.now-GPS_UTC_EPOCH_OFFSET)*1000))
        self.assertEqual(values['turf.income.waitingtime'],self.rules.income_interval*1000)
        self.crew('A','Clock crew',0,0)
        turf=self.map('A',0,0).findall('turflist/turf')[17]
        self.assertEqual(int(turf.findtext('timestamp')),0)
        db=self.s.connection()
        db.execute('UPDATE turfs SET stamp=? WHERE x=0 AND y=0',(self.now,));db.close()
        turf=self.map('A',0,0).findall('turflist/turf')[17]
        self.assertEqual(int(turf.findtext('timestamp')),wire_time(self.now))
        self.assertEqual(int(turf.findtext('lastcollect')),wire_time(self.now))
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1791586800.0
        self.rules=Rules(starting_money=500000,attack_delay=2)
        self.world=World();self.path=Path(self.temp.name)/'world.sqlite3'
        self.s=Service(self.path,self.rules,self.world,lambda:self.now)
        self.commands=set()
        # These fixtures model clients that completed the first-login form.
        for terminal in ('A','B','C','D'):
            self.s.handle('setuserinfo',encode_request(f'<user><terminalhardwareid>{terminal}</terminalhardwareid><colorsname>Player {terminal}</colorsname><money>{self.rules.starting_money}</money></user>'.encode()))
    def request(self,c,terminal='A',body='',attrs=''):
        root=COMMAND_ROOTS[c];self.commands.add(c)
        ident='' if c=='getganginfo' else f'<{"sendertermhwid" if c=="sendsmsmessage" else "terminalhardwareid"}>{terminal}</{"sendertermhwid" if c=="sendsmsmessage" else "terminalhardwareid"}>'
        xml=f'<{root} {attrs}>{ident}{body}</{root}>'.encode()
        return self.s.handle(c,encode_request(xml))
    def map(self,terminal='A',x=0,y=0):
        lat,lon=self.world.latlon(x*100+50,y*100+50)
        return self.request('getmapview',terminal,f'<xpos>{lon}</xpos><ypos>{lat}</ypos><metainfo>1</metainfo><map>1</map>')
    def buy(self,terminal,x,y):return self.request('buyturf',terminal,f'<turf>{x},{y}</turf><name>Turf {terminal}</name>')
    def crew(self,terminal,name,x,y):
        self.map(terminal,x,y)
        return self.request('creategang',terminal,f'<turf>{x},{y}</turf><turfname>{name} base</turfname><gang><color>65536,0,32768</color><name>{name}</name><tag>TEST</tag><password>secret</password></gang>')
    def money(self,t):return int(self.request('getuserinfo',t).findtext('money'))
    def test_global_players_share_stable_ids_without_dateline_duplicates(self):
        locations=[(35.68,139.69),(-33.87,151.21),(64.15,-21.94),(-34.6,-58.38),(0,179.9999),(0,-179.9999),(90,20),(-90,20)]
        for lat,lon in locations:
            result=self.request('getmapview','A',f'<xpos>{lon}</xpos><ypos>{lat}</ypos><map>1</map><metainfo>1</metainfo>')
            ids=[node.findtext('coordinates') for node in result.findall('turflist/turf')]
            self.assertEqual(len(set(ids)),35)
            x,y,*_=self.world.location(lat,lon)
            self.assertEqual(ids[17],f'{x},{y}')
        self.map('A')
        with self.assertRaises(GameError):self.buy('A',self.world.columns,0)

    def test_all_commands_two_players_and_persistent_world(self):
        self.request('getgamevars');self.request('synctimestamps');self.request('getuserinfo')
        self.request('setuserinfo','A','<colorsname>Alice</colorsname><money>500000</money><gangid>999</gangid>')
        a=self.crew('A','Alpha',0,0);gid=int(a.findtext('gang/id'))
        self.crew('B','Beta',1,0)
        self.map('A');self.buy('A',0,1)
        self.request('buyhomeboy','A','<turf>0,0</turf><homeboys><homeboy><type>1</type><number>2</number></homeboy></homeboys>')
        self.request('organizehomeboys','A','<turf><id>0,0</id><homeboys type="1" change="-1"/></turf><turf><id>0,1</id><homeboys type="1" change="1"/></turf>')
        self.request('setturfinfo','A','<turf>0,0</turf><name>New name</name>')
        self.request('setturfprice','A','<xpos>0</xpos><ypos>1</ypos><price>250</price>')
        self.map('B');self.buy('B',0,1)
        self.request('tagturf','B','<turf>0,0</turf><tag>RIVAL</tag>')
        self.request('sendsmsmessage','B','<recipienttermhwid>A</recipienttermhwid><message>Hello</message>')
        inbox=self.request('getmessages','A');self.assertGreaterEqual(len(inbox.findall('message')),2)
        self.assertEqual(len(self.request('getmessages','A').findall('message')),0)
        self.now+=86400;self.map('A');self.request('collectincome','A','<turf>0,0</turf>')
        self.request('getturflist','A',f'<gangid>{gid}</gangid>')
        self.request('getganginfo',body=f'<gangid>{gid}</gangid>')
        self.request('setganginfo','A',f'<gang><id>{gid}</id><gangleaderterminalhardwareid>A</gangleaderterminalhardwareid><name>Alpha2</name><color>0,65536,0</color><tag>new</tag><password>__UNCHANGED__</password></gang>')
        self.map('C')
        self.request('joingang','C','<turf>-1,0</turf><turfname>Third</turfname><gangname>Alpha2</gangname><password>secret</password>')
        self.request('abdicate','A','<newleadertermhwid>C</newleadertermhwid>')
        self.request('releasegangmember','C','<releaseehwid>A</releaseehwid>')
        self.map('D',-1,1)
        self.request('joingang','D','<turf>-1,1</turf><turfname>Fourth</turfname><gangname>Alpha2</gangname><password>secret</password>')
        self.request('leavegang','D')
        self.map('B');self.request('attackturf','B','<turf>0,0</turf>','action="initiate"')
        self.request('attackturf','B','<turf>0,0</turf>','action="cancel"')
        self.request('attackturf','B','<turf>0,0</turf>','action="initiate"');self.now+=3
        self.request('attackturf','B','<turf>0,0</turf>','action="commence"')
        result=self.request('attackturf','B','<turf>0,0</turf><outcome winner="attacker"><homeboyslost><homeboy><type>1</type><number>1</number></homeboy></homeboyslost></outcome>','action="report"')
        self.assertEqual(result.findtext('turf/state'),'normal')
        self.assertNotEqual(result.findtext('turf/gangid'),str(gid))
        self.request('dissolvegang','C');self.request('removeplayer','D')
        self.assertEqual(self.commands,set(COMMAND_ROOTS))
        reopened=Service(self.path,self.rules,self.world,lambda:self.now)
        before=self.request('getuserinfo','B').findtext('money')
        self.s=reopened;self.assertEqual(self.request('getuserinfo','B').findtext('money'),before)
    def test_map_order_offsets_and_owned_crew_are_present(self):
        self.crew('A','Alpha',0,0)
        response=self.map();cells=response.findall('turflist/turf')
        self.assertEqual(len(cells),35)
        self.assertEqual(cells[0].findtext('coordinates'),'-3,-2')
        self.assertEqual(cells[-1].findtext('coordinates'),'3,2')
        self.assertEqual(cells[17].findtext('coordinates'),'0,0')
        self.assertTrue(0<=int(response.findtext('xoffset'))<45)
        self.assertTrue(response.findall('gangs/gang'))
    def test_atomic_rollback_and_simultaneous_purchase(self):
        self.map('A');self.map('B');money=self.money('A')
        def attempt(t):
            try:self.buy(t,0,0);return t
            except GameError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(attempt,['A','B']))
        self.assertEqual(sum(r is not None for r in results),1)
        loser='A' if results[0] is None else 'B';self.assertEqual(self.money(loser),money)
    def test_invalid_moves_do_not_create_homeboys_or_charge(self):
        self.crew('A','Alpha',0,0);before=self.money('A')
        with self.assertRaises(GameError):self.request('organizehomeboys','A','<turf><id>0,0</id><homeboys type="1" change="1"/></turf>')
        with self.assertRaises(GameError):self.request('buyhomeboy','A','<turf>0,0</turf><homeboys><homeboy><type>1</type><number>99</number></homeboy></homeboys>')
        self.assertEqual(self.money('A'),before)
    def test_attack_countdown_lock_expiry_and_duplicate_report(self):
        self.crew('A','Alpha',0,0);self.crew('B','Beta',1,0);self.map('B')
        self.request('attackturf','B','<turf>0,0</turf>','action="initiate"');before=self.money('B')
        with self.assertRaises(GameError):self.request('attackturf','B','<turf>0,0</turf>','action="commence"')
        with self.assertRaises(GameError):self.request('tagturf','B','<turf>0,0</turf><tag>no</tag>')
        self.assertEqual(self.money('B'),before)
        self.now+=3;self.request('attackturf','B','<turf>0,0</turf>','action="commence"')
        body='<turf>0,0</turf><outcome winner="owner"><homeboyslost/></outcome>'
        self.request('attackturf','B',body,'action="report"')
        with self.assertRaises(GameError):self.request('attackturf','B',body,'action="report"')
        self.request('attackturf','B','<turf>0,0</turf>','action="initiate"');self.now+=self.rules.attack_expiry+1
        self.map('B');self.request('tagturf','B','<turf>0,0</turf><tag>yes</tag>')
    def test_income_is_not_double_collected(self):
        self.crew('A','Alpha',0,0);before=self.money('A');self.now+=2*86400;self.map()
        self.request('collectincome','A','<turf>0,0</turf>');self.request('collectincome','A','<turf>0,0</turf>')
        self.assertEqual(self.money('A')-before,2*self.rules.daily_income)
    def test_identity_proximity_permissions_and_wrong_password(self):
        crew=self.crew('A','Alpha',0,0);self.map('B');before=self.money('B')
        with self.assertRaises(GameError):self.buy('B',20,20)
        with self.assertRaises(GameError):self.request('setturfinfo','B','<turf>0,0</turf><name>hijack</name>')
        with self.assertRaises(GameError):self.request('dissolvegang','B')
        self.assertEqual(self.money('B'),before)
        self.buy('B',1,0)
        with self.assertRaises(GameError):self.request('joingang','B','<turf>1,0</turf><gangname>Alpha</gangname><password>wrong</password>')
        self.assertNotIn('secret',ET.tostring(crew).decode())
    def test_world_origin_cannot_move_existing_territories(self):
        with self.assertRaises(GameError):Service(self.path,self.rules,World(40,-3))
    def test_codec_envelope_and_hostile_xml(self):
        xml=b'<getuserinfo><terminalhardwareid>A</terminalhardwareid></getuserinfo>'
        self.assertEqual(decode_request(encode_request(xml)).tag,'getuserinfo')
        for data in ['<c>bad!</c>','<!DOCTYPE x><c>AA==</c>','<getuserinfo/>','<c>AA==</c>']:
            with self.assertRaises(ValueError):decode_request(data)
    def test_first_login_requests_player_name_without_issuing_real_money(self):
        terminal='Fresh'
        self.s.handle('getgamevars',encode_request(f'<getgamevars><terminalhardwareid>{terminal}</terminalhardwareid></getgamevars>'.encode()))
        with self.assertRaises(GameError) as failure:
            self.s.handle('getuserinfo',encode_request(f'<getuserinfo><terminalhardwareid>{terminal}</terminalhardwareid></getuserinfo>'.encode()))
        self.assertEqual(failure.exception.code,'NoSuchUserException')
        self.s.handle('setuserinfo',encode_request(f'<user><terminalhardwareid>{terminal}</terminalhardwareid><colorsname>New player</colorsname><money>43210</money></user>'.encode()))
        result=self.s.handle('getuserinfo',encode_request(f'<getuserinfo><terminalhardwareid>{terminal}</terminalhardwareid></getuserinfo>'.encode()))
        self.assertEqual(result.findtext('colorsname'),'New player')
        self.assertEqual(result.findtext('money'),'43210')

    def test_login_after_server_restart_preserves_registered_profile(self):
        self.crew('A','Persistent crew',0,0)
        before=ET.tostring(self.request('getuserinfo','A'))
        reopened=Service(self.path,self.rules,self.s.world)
        self.s=reopened
        self.request('getgamevars','A')
        self.request('synctimestamps','A')
        self.assertEqual(ET.tostring(self.request('getuserinfo','A')),before)
        self.assertEqual(self.request('getganginfo','A','<gangid>1</gangid>').findtext('name'),'Persistent crew')

    def test_native_first_turf_purchase_and_millisecond_rules(self):
        before=self.money('A');first=self.crew('A','Alpha',0,0)
        self.assertEqual(int(first.findtext('user/money')),before-self.rules.turf_price)
        self.assertEqual(first.findtext('turf/gangid'),first.findtext('gang/id'))
        root=self.request('getgamevars')
        values={v.findtext('name'):int(v.findtext('value')) for v in root.findall('var')}
        self.assertEqual(values['turf.timestamp.commence.delay'],self.rules.attack_delay*1000)
        self.map('B',1,0);before=self.money('B')
        with self.assertRaises(GameError):self.request('joingang','B','<turf>1,0</turf><gangname>Alpha</gangname><password>wrong</password><turfname>Beta</turfname>')
        self.assertEqual(self.money('B'),before)
        self.assertEqual(self.map('B',1,0).findall('turflist/turf')[17].findtext('gangid'),'0')
        joined=self.request('joingang','B','<turf>1,0</turf><gangname>Alpha</gangname><password>secret</password><turfname>Beta</turfname>')
        self.assertEqual(int(joined.findtext('user/money')),before-self.rules.turf_price)
        self.assertEqual(joined.findtext('turf/gangid'),first.findtext('gang/id'))

    def test_all_commands_over_http_two_players(self):
        def fetch(url):
            image=Image.new('RGB',(256,256),'#cfe3dc');out=io.BytesIO();image.save(out,'PNG');return out.getvalue()
        tiles=Tiles(self.world,'https://example.invalid/{z}/{x}/{y}.png',Path(self.temp.name)/'tiles',fetch=fetch)
        server=BoundedHTTPServer(('127.0.0.1',0),self.s,tiles)
        worker=threading.Thread(target=server.serve_forever);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(worker.join);self.addCleanup(server.shutdown)
        def http_request(c,terminal='A',body='',attrs=''):
            root=COMMAND_ROOTS[c];self.commands.add(c)
            key='sendertermhwid' if c=='sendsmsmessage' else 'terminalhardwareid'
            ident='' if c=='getganginfo' else f'<{key}>{terminal}</{key}>'
            xml=f'<{root} {attrs}>{ident}{body}</{root}>'.encode()
            encoded=urllib.parse.urlencode({'command':c,'data':encode_request(xml)}).encode()
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+ENDPOINT,encoded,
                                       {'Content-Type':'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req,timeout=20) as response:raw=response.read()
            if c=='getmapview':self.assertIn(b'<image-data><![CDATA[',raw)
            result=ET.fromstring(raw)
            self.assertNotEqual(result.tag,'error',ET.tostring(result).decode())
            return result
        self.request=http_request
        self.test_all_commands_two_players_and_persistent_world()

    def test_http_real_encoded_post_and_gif_provider_cache(self):
        calls=[]
        def fetch(url):
            calls.append(url);im=Image.new('RGB',(256,256),'#cfe3dc');out=io.BytesIO();im.save(out,'PNG');return out.getvalue()
        tiles=Tiles(self.world,'https://example.invalid/{z}/{x}/{y}.png',Path(self.temp.name)/'tiles',fetch=fetch)
        server=BoundedHTTPServer(('127.0.0.1',0),self.s,tiles)
        worker=threading.Thread(target=server.serve_forever);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(worker.join);self.addCleanup(server.shutdown)
        xml=b'<getmapview><terminalhardwareid>A</terminalhardwareid><xpos>2.3522</xpos><ypos>48.8566</ypos><map>1</map><metainfo>1</metainfo></getmapview>'
        url=f'http://127.0.0.1:{server.server_port}'+ENDPOINT
        body=urllib.parse.urlencode({'command':'getmapview','data':encode_request(xml)}).encode()
        req=urllib.request.Request(url,body,{'Content-Type':'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req,timeout=20) as response:root=ET.fromstring(response.read())
        self.assertEqual(root.tag,'getmapview')
        gif=base64.b64decode(root.findtext('image-data'));self.assertTrue(gif.startswith(b'GIF'))
        self.assertEqual(Image.open(io.BytesIO(gif)).size,(315,225))
        first=len(calls);self.assertGreater(first,0)
        with urllib.request.urlopen(req,timeout=20) as response:response.read()
        self.assertEqual(len(calls),first)

if __name__=='__main__':unittest.main()
