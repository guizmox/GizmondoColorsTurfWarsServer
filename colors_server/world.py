"""Stable periodic geographic grid for a worldwide world; GIF background from OSM raster tiles.
Original server projection is unknown. This replacement uses an explicit fixed
reference latitude, never a per-request origin (which would move territory IDs).
"""
import email.utils
import hashlib
import io
import json
import math
import os
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw

RADIUS = 6378137.0
CELL = 100.0
PIXELS = 45
WIDTH, HEIGHT = 315, 225

class World:
    def __init__(self, latitude=48.8566, longitude=2.3522):
        if not (-75 < latitude < 75 and -180 <= longitude <= 180):
            raise ValueError('Invalid world origin')
        self.latitude, self.longitude = latitude, longitude
        self.scale = math.cos(math.radians(latitude))
        self.columns = round(2*math.pi*RADIUS*self.scale/CELL)
        self.xstep = 2*math.pi*RADIUS*self.scale/self.columns

    def meters(self, latitude, longitude):
        if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError('Invalid GPS coordinates')
        return (RADIUS * math.radians((longitude-self.longitude+180)%360-180) * self.scale,
                RADIUS * math.radians(latitude-self.latitude))

    def latlon(self, x, y):
        return (self.latitude + math.degrees(y/RADIUS),
                self.longitude + math.degrees(x/(RADIUS*self.scale)))

    def location(self, latitude, longitude):
        x, y = self.meters(latitude, longitude)
        return self.wrap(math.floor(x/self.xstep)), math.floor(y/CELL), int((x%self.xstep)*PIXELS/self.xstep), int((y%CELL)*PIXELS/CELL)

    def wrap(self,x):
        return (x+self.columns//2)%self.columns-self.columns//2

    def xdistance(self,a,b):
        return abs(self.wrap(a-b))

    def coordinates(self, x, y):
        return [(self.wrap(x+dx),y+dy) for dy in range(-2,3) for dx in range(-3,4)]

    def bounds(self, x, y):
        # Client consumes south-to-north rows; image itself is north-up.
        return (self.xstep*(x-3), CELL*(y-2), self.xstep*(x+4), CELL*(y+3))

class Tiles:
    def __init__(self, world, url, cache, agent='ColorsRebuilt/0.1', fetch=None, max_cache_bytes=256*1024*1024, max_cache_files=4096):
        if not url.startswith('https://'):
            raise ValueError('Tile source must use HTTPS')
        self.world, self.url, self.agent = world, url, agent
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        if max_cache_bytes <= 0 or max_cache_files <= 0:
            raise ValueError('Invalid tile cache limits')
        self.max_cache_bytes, self.max_cache_files = max_cache_bytes, max_cache_files
        self.lock = threading.RLock()
        self.fetch = fetch
        self.prune_cache()

    def tile(self, z, x, y):
        # Serialize cache mutations and duplicate downloads. Waiting requests do
        # not hold gameplay transactions; HTTP concurrency is independently bounded.
        if not self.lock.acquire(timeout=5):
            raise TimeoutError('Tile cache busy')
        try:
            return self._tile(z, x, y)
        finally:
            self.lock.release()

    def prune_cache(self):
        entries = []
        for path in self.cache.glob('*.png'):
            stat = path.stat()
            meta = path.with_suffix('.json')
            size = stat.st_size + (meta.stat().st_size if meta.exists() else 0)
            entries.append((stat.st_mtime, path, size))
        total = sum(e[2] for e in entries)
        count = len(entries)
        for _, path, size in sorted(entries):
            if total <= self.max_cache_bytes and count <= self.max_cache_files:
                break
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            total -= size
            count -= 1

    def _tile(self, z, x, y):
        x %= 1<<z
        if not 0 <= y < 1<<z:
            raise ValueError('Tile latitude outside coverage')
        url = self.url.format(z=z,x=x,y=y)
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest()+'.png')
        metadata=path.with_suffix('.json')
        now=time.time();cached={}
        if path.exists():
            try:cached=json.loads(metadata.read_text())
            except (OSError,ValueError):cached={'expires':path.stat().st_mtime+7*86400}
            if not isinstance(cached,dict):cached={}
            if now<float(cached.get('expires',0)):
                with Image.open(path) as image:
                    if image.size != (256, 256):
                        raise ValueError('Expected 256px raster tiles')
                    return image.convert('RGB')
        headers={};unchanged=False
        if self.fetch:
            raw = self.fetch(url)
        else:
            request_headers={'User-Agent':self.agent}
            if cached.get('etag'):request_headers['If-None-Match']=cached['etag']
            if cached.get('modified'):request_headers['If-Modified-Since']=cached['modified']
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    headers=response.headers;raw=response.read(1024*1024+1)
            except urllib.error.HTTPError as exc:
                if exc.code!=304 or not path.exists():raise
                headers=exc.headers;raw=path.read_bytes();unchanged=True
        # Honour the provider's caching headers; use the OSM seven-day
        # fallback only when no expiration is supplied.
        expires=now+7*86400
        policy=headers.get('Cache-Control','')
        directives={item.split('=',1)[0].strip().lower():item.split('=',1)[1].strip().strip('\"')
                    for item in policy.split(',') if '=' in item}
        if 'max-age' in directives:
            try:expires=now+max(0,int(directives['max-age'])-int(headers.get('Age','0')))
            except ValueError:pass
        elif headers.get('Expires'):
            try:expires=email.utils.parsedate_to_datetime(headers['Expires']).timestamp()
            except (ValueError,TypeError):pass
        if 'no-store' in policy.lower() or 'no-cache' in policy.lower():expires=now
        if len(raw) > 1024*1024:
            raise ValueError('Tile exceeds size limit')
        with Image.open(io.BytesIO(raw)) as source:
            if source.size != (256,256):
                raise ValueError('Expected 256px raster tiles')
            source.load()
            im = source.convert('RGB')
        # Atomic replacement; parallel viewers never see an incomplete PNG.
        import tempfile
        temp = info_temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache, suffix='.png', delete=False) as f:
                temp = Path(f.name)
                im.save(f, format='PNG')
            os.replace(temp,path)
            cache_info={'expires':expires,'etag':headers.get('ETag',cached.get('etag','') if unchanged else ''),
                        'modified':headers.get('Last-Modified',cached.get('modified','') if unchanged else '')}
            with tempfile.NamedTemporaryFile(dir=self.cache,suffix='.json',mode='w',delete=False) as f:
                info_temp=Path(f.name);json.dump(cache_info,f)
            os.replace(info_temp,metadata)
        finally:
            if temp is not None:temp.unlink(missing_ok=True)
            if info_temp is not None:info_temp.unlink(missing_ok=True)
        if 'no-store' in policy.lower():
            path.unlink(missing_ok=True);metadata.unlink(missing_ok=True)
        self.prune_cache()
        return im

    @staticmethod
    def pixel(latitude,longitude,z):
        n=256*(1<<z)
        return ((longitude+180)/360*n, (1-math.asinh(math.tan(math.radians(latitude)))/math.pi)/2*n)

    def render(self, x, y):
        west,south,east,north = self.world.bounds(x,y)
        lat_n,lon_w=self.world.latlon(west,north)
        lat_s,lon_e=self.world.latlon(east,south)
        limit=85.0511287798066
        clipped_n=min(limit,max(-limit,lat_n));clipped_s=min(limit,max(-limit,lat_s))
        if clipped_n<=clipped_s:
            return self.polar_map()
        z=16
        px0,py0=self.pixel(clipped_n,lon_w,z);px1,py1=self.pixel(clipped_s,lon_e,z)
        while (math.floor(px1/256)-math.floor(px0/256)+1)*(math.floor(py1/256)-math.floor(py0/256)+1)>16 and z>0:
            z-=1
            px0,py0=self.pixel(clipped_n,lon_w,z);px1,py1=self.pixel(clipped_s,lon_e,z)
        tx0,ty0=math.floor(px0/256),math.floor(py0/256)
        tx1,ty1=math.floor(px1/256),math.floor(py1/256)
        if (tx1-tx0+1)*(ty1-ty0+1) > 16:
            raise ValueError('Map needs too many tiles; move the world origin closer')
        mosaic=Image.new('RGB',((tx1-tx0+1)*256,(ty1-ty0+1)*256))
        deadline = time.monotonic()+30
        for ty in range(ty0,ty1+1):
            for tx in range(tx0,tx1+1):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Map fetch deadline exceeded')
                mosaic.paste(self.tile(z,tx,min((1<<z)-1,max(0,ty))),(256*(tx-tx0),256*(ty-ty0)))
        # Resample each row against latitude so grid boundaries and the map use
        # the same metric projection rather than a distorted Mercator crop.
        out=Image.new('RGB',(WIDTH,HEIGHT),'#e8edf0')
        for row in range(HEIGHT):
            lat,_=self.world.latlon(west,north-(row+.5)*(north-south)/HEIGHT)
            if not -limit<lat<limit:continue
            _,py=self.pixel(lat,lon_w,z)
            strip=mosaic.transform((WIDTH,1),Image.Transform.EXTENT,
                (px0-tx0*256,py-ty0*256-.5,px1-tx0*256,py-ty0*256+.5),Image.Resampling.BILINEAR)
            out.paste(strip,(0,row))
        return self.gif(out)

    def polar_map(self):
        out=Image.new('RGB',(WIDTH,HEIGHT),'#e8edf0')
        ImageDraw.Draw(out).text((8,8),'Outside OSM raster coverage',fill='black')
        return self.gif(out)

    @staticmethod
    def gif(out):
        draw=ImageDraw.Draw(out)
        text='(c) OpenStreetMap contributors'
        box=draw.textbbox((0,0),text);tw=box[2]-box[0]
        # Keep attribution above the game's bottom HUD, which overlays the GIF.
        top=HEIGHT-30
        draw.rectangle((WIDTH-tw-4,top,WIDTH,top+14),fill='white')
        draw.text((WIDTH-tw-2,top+1),text,fill='black')
        data=io.BytesIO();out.quantize(colors=256).save(data,format='GIF')
        return data.getvalue()
