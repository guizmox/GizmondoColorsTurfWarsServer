import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from colors_server.codec import encode_request
from colors_server.service import Service, GameError
from colors_server.world import World

class ClientCoordinatesTests(unittest.TestCase):
    def test_global_map_ids_are_positive_unique_and_round_trip(self):
        world=World()
        for lat,lon in [(48.8584,2.2945),(48.8566,2.3522),(-33.87,151.21),(0,179.9999),(0,-179.9999),(90,20),(-90,20)]:
            x,y,*_=world.location(lat,lon)
            cells=world.coordinates(x,y)
            wire=[world.client_coordinates(*xy) for xy in cells]
            self.assertEqual(len(set(wire)),35)
            self.assertTrue(all(a>0 and b>0 for a,b in wire))
            self.assertEqual([world.storage_coordinates(*xy) for xy in wire],cells)
        for pair in [(0,1),(1,0),(-1,1),(1,-1)]:
            with self.assertRaises(ValueError):world.storage_coordinates(*pair)

    def test_echoed_map_id_buys_same_persisted_paris_turf(self):
        # No fixture translation helper: echo the actual response's client ID.
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'world.db';world=World();service=Service(path,world=world)
            def request(command,body):
                return service.handle(command,encode_request(body.encode()))
            request('setuserinfo','<user><terminalhardwareid>Paris-test</terminalhardwareid><colorsname>Paris test</colorsname><money>100000</money></user>')
            body='<getmapview><terminalhardwareid>Paris-test</terminalhardwareid><xpos>2.2945</xpos><ypos>48.8584</ypos><map>1</map><metainfo>1</metainfo></getmapview>'
            view=request('getmapview',body)
            x,y,*_=world.location(48.8584,2.2945)
            with service.connection() as db:
                before=[tuple(row) for row in db.execute('SELECT x,y FROM turfs ORDER BY y,x')]
            wire=view.findall('turflist/turf')[17].findtext('coordinates')
            self.assertTrue(all(int(v)>0 for v in wire.split(',')))
            out=request('buyturf',f'<buyturf><terminalhardwareid>Paris-test</terminalhardwareid><turf>{wire}</turf><name>Paris home</name></buyturf>')
            self.assertEqual(out.findtext('turf/coordinates'),wire)
            with service.connection() as db:
                self.assertEqual(tuple(db.execute('SELECT x,y FROM turfs WHERE holder=?',('Paris-test',)).fetchone()),(x,y))
            service=Service(path,world=world)
            self.assertEqual(request('getmapview',body).findall('turflist/turf')[17].findtext('coordinates'),wire)
            with service.connection() as db:
                self.assertEqual([tuple(row) for row in db.execute('SELECT x,y FROM turfs ORDER BY y,x')],before)
            with self.assertRaises(GameError):
                request('buyturf',f'<buyturf><terminalhardwareid>Paris-test</terminalhardwareid><turf>{x},{y}</turf><name>Legacy</name></buyturf>')
            request('creategang',f'<creategang><terminalhardwareid>Paris-test</terminalhardwareid><turf>{wire}</turf><turfname>Paris home</turfname><gang><name>Paris crew</name><password>test</password><color>65536,0,0</color><tag>Paris</tag></gang></creategang>')
            price=12345
            request('setturfprice',f'<turfprice><terminalhardwareid>Paris-test</terminalhardwareid><xpos>{wire.split(",")[0]}</xpos><ypos>{wire.split(",")[1]}</ypos><price>{price}</price></turfprice>')
            with service.connection() as db:
                self.assertEqual(db.execute('SELECT price FROM turfs WHERE x=? AND y=?',(x,y)).fetchone()[0],price)

    def test_http_map_renderer_uses_storage_cells_not_positive_wire_ids(self):
        import io
        import threading
        import urllib.parse
        import urllib.request
        from PIL import Image
        from colors_server.http import BoundedHTTPServer, ENDPOINT
        world=World();calls=[]
        class MapRenderer:
            def render(self,x,y):
                calls.append((x,y));out=io.BytesIO()
                Image.new('RGB',(315,225),'blue').save(out,'GIF')
                return out.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            service=Service(Path(folder)/'world.db',world=world)
            server=BoundedHTTPServer(('127.0.0.1',0),service,MapRenderer())
            worker=threading.Thread(target=server.serve_forever);worker.start()
            try:
                xml=b'<getmapview><terminalhardwareid>Paris-map-test</terminalhardwareid><xpos>2.2945</xpos><ypos>48.8584</ypos><map>1</map><metainfo>1</metainfo></getmapview>'
                form=urllib.parse.urlencode({'command':'getmapview','data':encode_request(xml)}).encode()
                request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+ENDPOINT,form,{'Content-Type':'application/x-www-form-urlencoded'})
                with urllib.request.urlopen(request,timeout=5) as response:raw=response.read()
                self.assertIn(b'<image-data><![CDATA[',raw)
                self.assertEqual(calls,[tuple(world.location(48.8584,2.2945)[:2])])
                view=ET.fromstring(raw)
                self.assertEqual(len(view.findall('turflist/turf')),35)
                self.assertTrue(all(all(int(v)>0 for v in turf.findtext('coordinates').split(',')) for turf in view.findall('turflist/turf')))
            finally:
                server.shutdown();worker.join();server.server_close()
