import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from colors_server.codec import parse_xml
from colors_server.world import Tiles, World

class HardeningTests(unittest.TestCase):
    def test_xml_depth_and_attributes(self):
        with self.assertRaises(ValueError):parse_xml(b'<a>'*65+b'</a>'*65)
        with self.assertRaises(ValueError):parse_xml(('<a '+' '.join(f'x{i}="1"' for i in range(65))+'/>').encode())
        self.assertEqual(parse_xml(b'<a><b>normal</b></a>').findtext('b'),'normal')
    def test_cache_limits_and_image_dimensions(self):
        with tempfile.TemporaryDirectory() as d:
            data=io.BytesIO();Image.new('RGB',(256,256),'green').save(data,'PNG')
            t=Tiles(World(),'https://example.invalid/{z}/{x}/{y}',d,fetch=lambda _:data.getvalue(),max_cache_files=2)
            for x in range(5):t.tile(4,x,0)
            self.assertLessEqual(len(list(Path(d).glob('*.png'))),2)
            self.assertLessEqual(len(list(Path(d).glob('*.json'))),2)
            t.max_cache_bytes=1;t.tile(4,6,0)
            self.assertFalse(list(Path(d).glob('*.png')))
            bad=io.BytesIO();Image.new('RGB',(257,256)).save(bad,'PNG');t.fetch=lambda _:bad.getvalue()
            with self.assertRaises(ValueError):t.tile(4,7,0)

    def test_failed_begin_preserves_sqlite_busy_error(self):
        import sqlite3
        from colors_server.service import Service
        from colors_server.codec import encode_request
        with tempfile.TemporaryDirectory() as d:
            service=Service(Path(d)/'world.db')
            owner=service.connection();owner.execute('BEGIN IMMEDIATE')
            original=service.connection
            def fast_connection():
                db=original();db.execute('PRAGMA busy_timeout=0');return db
            service.connection=fast_connection
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                    service.handle('getgamevars',encode_request(b'<getgamevars><terminalhardwareid>A</terminalhardwareid></getgamevars>'))
            finally:
                owner.rollback();owner.close()
