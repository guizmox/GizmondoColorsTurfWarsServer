import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from colors_server.world import Tiles, World

class TileCacheTests(unittest.TestCase):
    def test_worldwide_maps_wrap_and_cover_remote_cities(self):
        raw=io.BytesIO();Image.new('RGB',(256,256),'blue').save(raw,format='PNG')
        world=World();calls=[]
        def fetch(url):calls.append(url);return raw.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            tiles=Tiles(world,'https://example.test/{z}/{x}/{y}.png',folder,fetch=fetch)
            for lat,lon in [(48.86,2.35),(35.68,139.69),(-33.87,151.21),(64.15,-21.94),(-34.6,-58.38),(84,15),(0,179.9999),(0,-179.9999),(89,20),(-89,20)]:
                x,y,*_=world.location(lat,lon)
                gif=Image.open(io.BytesIO(tiles.render(x,y)))
                self.assertEqual(gif.size,(315,225))
                self.assertEqual(len(set(world.coordinates(x,y))),35)
            a=world.location(0,179.9999)[0];b=world.location(0,-179.9999)[0]
            self.assertLessEqual(world.xdistance(a,b),1)
            edge=world.columns//2-1
            self.assertEqual(world.xdistance(world.wrap(edge+1),edge),1)
            self.assertIn((world.wrap(edge+1),0),world.coordinates(edge,0))
            self.assertGreater(len(calls),0)

    def test_expiration_etag_revalidation_and_no_store(self):
        raw=io.BytesIO();Image.new('RGB',(256,256),'blue').save(raw,format='PNG')
        class Response(io.BytesIO):
            headers={'Cache-Control':'max-age=60','ETag':'"v1"'}
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with tempfile.TemporaryDirectory() as folder:
            tiles=Tiles(World(),'https://example.test/{z}/{x}/{y}.png',folder)
            with patch('colors_server.world.urllib.request.urlopen',return_value=Response(raw.getvalue())) as fetch:
                tiles.tile(16,1,1);tiles.tile(16,1,1)
                self.assertEqual(fetch.call_count,1)
            meta=next(Path(folder).glob('*.json'));data=json.loads(meta.read_text());data['expires']=0
            meta.write_text(json.dumps(data))
            def unchanged(request,timeout):
                self.assertEqual(request.get_header('If-none-match'),'"v1"')
                raise urllib.error.HTTPError(request.full_url,304,'unchanged',{'Cache-Control':'max-age=120'},None)
            with patch('colors_server.world.urllib.request.urlopen',side_effect=unchanged):
                self.assertEqual(tiles.tile(16,1,1).getpixel((0,0)),(0,0,255))
            self.assertGreater(json.loads(meta.read_text())['expires'],0)
            data['expires']=0;meta.write_text(json.dumps(data))
            response=Response(raw.getvalue());response.headers={'Cache-Control':'no-store'}
            with patch('colors_server.world.urllib.request.urlopen',return_value=response):tiles.tile(16,1,1)
            self.assertEqual(list(Path(folder).iterdir()),[])
