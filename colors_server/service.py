"""Transactional reconstruction of Colors Turf War commands.
Wire fields are recovered from the client; absent historical rules have explicit
configurable values. The client reports battles and single-player money itself.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
GPS_UTC_EPOCH_OFFSET = 63072000

def wire_time(seconds):
    """Colors compares its server clock directly with GPS1's 1972 UTC clock.

    Keep database/audit times in Unix seconds; convert absolute wire dates only.
    Zero remains the protocol's unset-date sentinel. Durations are unchanged.
    """
    return int((seconds-GPS_UTC_EPOCH_OFFSET)*1000) if seconds else 0
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from .codec import decode_request
from .world import World

COMMAND_ROOTS = {
 'getgamevars':'getgamevars','synctimestamps':'synctimestamps','getuserinfo':'getuserinfo',
 'setuserinfo':'user','getmapview':'getmapview','buyturf':'buyturf','creategang':'creategang',
 'joingang':'joingang','leavegang':'leavegang','dissolvegang':'dissolvegang',
 'removeplayer':'removeplayer','getganginfo':'getganginfo','setganginfo':'setganginfo',
 'abdicate':'abdicate','releasegangmember':'releasegangmember','getturflist':'turfrequest',
 'setturfprice':'turfprice','setturfinfo':'setturfinfo','tagturf':'tagturf',
 'buyhomeboy':'buyhomeboy','organizehomeboys':'organizehomeboys','collectincome':'collectincome',
 'attackturf':'turfattack','sendsmsmessage':'sendsmsmessage','getmessages':'getmessages'}
WEAPONS = 'smith american_180 striker spectre magnum m14 ak47 sawed_shotgun ithaka mp5 grenade nailgun buger ingram molotov uzi deagle browning'.split()

@dataclass
class Rules:
    starting_money:int=30000
    turf_price:int=30000
    homeboy_prices:tuple=(2000,5000,7000)
    homeboy_limits:tuple=(10,10,10)
    attack_price:int=1001
    attack_delay:int=120
    attack_expiry:int=600
    daily_income:int=1500
    income_cap:int=10000
    income_interval:int=86400
    position_age:int=300
    sync_single_player_money:bool=True
    player_health:int=100
    homeboy_health:tuple=(100,100,100)
    homeboy_aim:tuple=(100,100,100)
    weapon_prices:dict=field(default_factory=lambda:{weapon:100 for weapon in WEAPONS})

    def __post_init__(self):
        for name in ('starting_money','turf_price','attack_price','attack_delay','attack_expiry',
                     'daily_income','income_cap','income_interval','position_age','player_health'):
            value=getattr(self,name)
            if type(value) is not int or not 0<=value<=2147483647:raise ValueError('Invalid rule '+name)
        if self.income_interval==0 or self.attack_expiry<=self.attack_delay or self.position_age==0:
            raise ValueError('Invalid rule timing')
        if self.attack_delay>2147483:raise ValueError('Attack delay cannot fit the client millisecond field')
        for name in ('homeboy_prices','homeboy_limits','homeboy_health','homeboy_aim'):
            values=getattr(self,name)
            if len(values)!=3 or any(type(value) is not int or not 0<=value<=2147483647 for value in values):
                raise ValueError('Invalid rule '+name)
        if type(self.sync_single_player_money) is not bool:raise ValueError('Invalid money synchronization rule')
        if set(self.weapon_prices)!=set(WEAPONS) or any(type(value) is not int or not 0<=value<=2147483647 for value in self.weapon_prices.values()):
            raise ValueError('Specify a valid price for every weapon')

class GameError(ValueError):
    def __init__(self,message,code='IllegalOperationException'):
        super().__init__(message);self.code=code

def text(root,path,default=None,maxlen=1024):
    node=root.find(path)
    value=node.text if node is not None and node.text is not None else default
    if value is None or len(value)>maxlen:
        raise GameError('Missing or oversized '+path)
    return value

def number(root,path,default=None,minimum=0,maximum=2147483647):
    try: value=int(text(root,path,str(default) if default is not None else None))
    except (ValueError,TypeError) as exc: raise GameError('Invalid '+path) from exc
    if not minimum<=value<=maximum:raise GameError('Out of range '+path)
    return value

def add(root,key,value):
    node=ET.SubElement(root,key);node.text=str(value);return node

def pair(value):
    try:
        x,y=map(int,value.split(','))
        if abs(x)>10000000 or abs(y)>10000000:raise ValueError()
        return x,y
    except ValueError as exc:raise GameError('Invalid turf coordinates') from exc

def password_hash(password):
    salt=secrets.token_bytes(16)
    return salt.hex()+':'+hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()

def password_matches(password,stored):
    salt,digest=stored.split(':')
    return hmac.compare_digest(hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex(),digest)

class Service:
    def __init__(self,database,rules=None,world=None,clock=time.time):
        self.path=str(database);self.rules=rules or Rules();self.world=world or World();self.clock=clock
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with contextlib.closing(self.connection()) as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
              terminal TEXT PRIMARY KEY,name TEXT NOT NULL,money INTEGER NOT NULL CHECK(money>=0),
              gang INTEGER NOT NULL DEFAULT 0,lat REAL,lon REAL,position_time REAL,
              stamp INTEGER NOT NULL DEFAULT 0,ack INTEGER NOT NULL DEFAULT 0,
              registered INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS gangs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE COLLATE NOCASE,
              leader TEXT NOT NULL,color TEXT NOT NULL,tag TEXT NOT NULL,password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turfs(
              x INTEGER NOT NULL,y INTEGER NOT NULL,name TEXT NOT NULL DEFAULT 'Unnamed',
              gang INTEGER NOT NULL DEFAULT 0,holder TEXT NOT NULL DEFAULT '',price INTEGER NOT NULL DEFAULT 0,
              tag TEXT NOT NULL DEFAULT '',lastcollect REAL NOT NULL,stamp REAL NOT NULL DEFAULT 0,
              hb1 INTEGER NOT NULL DEFAULT 0 CHECK(hb1>=0),hb2 INTEGER NOT NULL DEFAULT 0 CHECK(hb2>=0),
              hb3 INTEGER NOT NULL DEFAULT 0 CHECK(hb3>=0),PRIMARY KEY(x,y));
            CREATE TABLE IF NOT EXISTS attacks(
              x INTEGER NOT NULL,y INTEGER NOT NULL,attacker TEXT NOT NULL,owner INTEGER NOT NULL,
              state TEXT NOT NULL,created REAL NOT NULL,ready REAL NOT NULL,expires REAL NOT NULL,
              PRIMARY KEY(x,y));
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,recipient TEXT NOT NULL,sender TEXT NOT NULL,
              subject TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS inbox ON messages(recipient,delivered,id);
            CREATE INDEX IF NOT EXISTS turf_gang ON turfs(gang);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,terminal TEXT NOT NULL,
              command TEXT NOT NULL);
            ''')
            # Databases from an earlier reconstruction already contain usable
            # named profiles. Preserve those when adding first-login state.
            if 'registered' not in {row['name'] for row in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN registered INTEGER NOT NULL DEFAULT 1')
            origin=json.dumps([self.world.latitude,self.world.longitude])
            previous=db.execute("SELECT value FROM settings WHERE key='world_origin'").fetchone()
            if previous and previous[0]!=origin:raise GameError('World origin cannot change in an existing database')
            db.execute("INSERT OR IGNORE INTO settings VALUES('world_origin',?)",(origin,))

    def connection(self):
        db=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def user(self,db,terminal):
        row=db.execute('SELECT * FROM users WHERE terminal=?',(terminal,)).fetchone()
        if row is None:
            db.execute('INSERT INTO users(terminal,name,money) VALUES(?,?,?)',
                (terminal,'Player-'+terminal[-8:],self.rules.starting_money))
            row=db.execute('SELECT * FROM users WHERE terminal=?',(terminal,)).fetchone()
        return row

    def turf(self,db,xy):
        if self.world.wrap(xy[0])!=xy[0]:raise GameError('Noncanonical longitude cell')
        latitude,_=self.world.latlon(0,(xy[1]+.5)*100)
        # Up to three display rows can extend past a pole in the 7x5 window.
        if not -90.003<=latitude<=90.003:raise GameError('Territory outside the globe')
        row=db.execute('SELECT * FROM turfs WHERE x=? AND y=?',xy).fetchone()
        if row is None:
            db.execute('INSERT INTO turfs(x,y,lastcollect) VALUES(?,?,?)',(*xy,self.clock()))
            row=db.execute('SELECT * FROM turfs WHERE x=? AND y=?',xy).fetchone()
        return row

    def get_gang(self,db,user):
        gang=db.execute('SELECT * FROM gangs WHERE id=?',(user['gang'],)).fetchone()
        if gang is None:raise GameError('Join a crew first','UserIsNotGangMemberException')
        return gang

    def leader(self,db,user):
        gang=self.get_gang(db,user)
        if gang['leader']!=user['terminal']:raise GameError('Only the crew leader may do this','NoGangManipulationException')
        return gang

    def nearby(self,user,xy):
        if user['position_time'] is None or self.clock()-user['position_time']>self.rules.position_age:
            raise GameError('Refresh your GPS map first')
        x,y,_,_=self.world.location(user['lat'],user['lon'])
        if max(self.world.xdistance(x,xy[0]),abs(y-xy[1]))>1:raise GameError('Turf is not adjacent to your position')

    def owned(self,user,turf):
        if user['gang']==0 or turf['gang']!=user['gang']:raise GameError('This turf is not controlled by your crew','NoGangManipulationException')

    def unlocked(self,db,xy):
        if db.execute('SELECT 1 FROM attacks WHERE x=? AND y=?',xy).fetchone():raise GameError('Turf is locked by an attack','TurfLockedForManipulationException')

    def pay(self,db,user,amount):
        if amount<0 or user['money']<amount:raise GameError('Insufficient funds')
        db.execute('UPDATE users SET money=money-? WHERE terminal=?',(amount,user['terminal']))

    def credit(self,db,terminal,amount):
        db.execute('UPDATE users SET money=MIN(2147483647,money+?) WHERE terminal=?',(amount,terminal))

    def user_xml(self,user):
        node=ET.Element('user')
        for k,v in [('colorsname',user['name']),('terminalhardwareid',user['terminal']),('gangid',user['gang']),
                    ('money',user['money']),('health',self.rules.player_health),('accuracy',100),('respect',100)]:add(node,k,v)
        return node

    def gang_xml(self,gang):
        node=ET.Element('gang')
        for k,v in [('id',gang['id']),('gangleaderterminalhardwareid',gang['leader']),('name',gang['name']),
                    ('tag',gang['tag'] or gang['name']),('color',gang['color']),('password','__UNCHANGED__')]:add(node,k,v)
        return node

    def client_pair(self, value):
        try:
            return self.world.storage_coordinates(*pair(value))
        except ValueError as exc:
            raise GameError('Invalid client turf coordinates; refresh the map') from exc

    def turf_xml(self,db,turf):
        node=ET.Element('turf')
        attack=db.execute('SELECT * FROM attacks WHERE x=? AND y=?',(turf['x'],turf['y'])).fetchone()
        state=('underattack' if attack['state']=='initiate' else 'fightinprogress') if attack else 'normal'
        for k,v in [('name',turf['name']),('tag',turf['tag']),('gangid',turf['gang']),('price',self.rules.turf_price if not turf['gang'] and not turf['holder'] else turf['price']),
                    ('state',state),('lastcollect',wire_time(turf['lastcollect'])),
                    ('timestamp',wire_time(attack['ready'] if attack else turf['stamp'])),
                    ('coordinates',','.join(map(str,self.world.client_coordinates(turf['x'],turf['y']))))]:add(node,k,v)
        boys=ET.SubElement(node,'homeboys')
        for level in (1,2,3):
            boy=ET.SubElement(boys,'homeboy');add(boy,'type',level);add(boy,'value',turf['hb'+str(level)])
        return node

    def bundle(self,db,command,user,xy=None,gang=False,all_turfs=False):
        node=ET.Element(command)
        node.append(self.user_xml(self.user(db,user['terminal'])))
        if xy is not None:node.append(self.turf_xml(db,self.turf(db,xy)))
        if gang:
            row=db.execute('SELECT * FROM gangs WHERE id=?',(self.user(db,user['terminal'])['gang'],)).fetchone()
            if row:node.append(self.gang_xml(row))
        if all_turfs:
            group=ET.SubElement(node,'turflist')
            rows=db.execute('SELECT * FROM turfs WHERE gang=? AND gang>0 ORDER BY y,x',(user['gang'],))
            for row in rows:group.append(self.turf_xml(db,row))
        return node

    def notify(self,db,user,gang_id,subject,body):
        gang=db.execute('SELECT leader FROM gangs WHERE id=?',(gang_id,)).fetchone()
        if gang and gang['leader']!=user['terminal']:
            db.execute('INSERT INTO messages(recipient,sender,subject,body,created) VALUES(?,?,?,?,?)',
                (gang['leader'],user['name'],subject,body,self.clock()))

    def dissolve(self,db,gang_id):
        db.execute('DELETE FROM attacks WHERE owner=? OR attacker IN (SELECT terminal FROM users WHERE gang=?)',(gang_id,gang_id))
        db.execute("UPDATE turfs SET gang=0,holder='',price=0,tag='',hb1=0,hb2=0,hb3=0 WHERE gang=?",(gang_id,))
        db.execute('UPDATE users SET gang=0 WHERE gang=?',(gang_id,))
        db.execute('DELETE FROM gangs WHERE id=?',(gang_id,))

    def handle(self,command,data):
        if command not in COMMAND_ROOTS:raise GameError('Unknown command','NoSuchCommandException')
        root=decode_request(data)
        if root.tag!=COMMAND_ROOTS[command]:raise GameError('Command and XML root disagree')
        terminal=text(root,'sendertermhwid' if command=='sendsmsmessage' else 'terminalhardwareid',None,maxlen=128) if command!='getganginfo' else None
        if terminal is not None and (not terminal or any(ord(c)<32 for c in terminal)):raise GameError('Invalid terminal identity')
        db=self.connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attacks WHERE expires<=?',(self.clock(),))
            user=self.user(db,terminal) if terminal else None
            result=self.dispatch(db,command,root,user)
            # Limit inbox and audit history; do not retain GPS/XML in logs.
            if terminal:
                db.execute('INSERT INTO audit(created,terminal,command) VALUES(?,?,?)',(self.clock(),terminal,command))
                add(result,'unreadmessages',db.execute('SELECT COUNT(*) FROM messages WHERE recipient=? AND delivered=0',(terminal,)).fetchone()[0])
            db.execute('DELETE FROM audit WHERE id<(SELECT COALESCE(MAX(id),0)-10000 FROM audit)')
            db.execute('DELETE FROM messages WHERE created<?',(self.clock()-30*86400,))
            db.execute('COMMIT')
            return result
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:db.close()

    def dispatch(self,db,c,r,u):
        if c=='getgamevars':
            out=ET.Element(c)
            add(out,'timestamp.current',u['ack']);add(out,'timestamp.incremented',u['stamp'])
            values={'server.time':wire_time(self.clock()),'attackprice':self.rules.attack_price,
                    'turf.income.waitingtime':self.rules.income_interval*1000,
                    'turf.timestamp.commence.delay':self.rules.attack_delay*1000,'health':self.rules.player_health}
            for i in (1,2,3):
                values[f'hb_level{i}_limit']=self.rules.homeboy_limits[i-1]
                values[f'homeboy.price.type{i}']=self.rules.homeboy_prices[i-1]
                values[f'homeboy.level{i}.health']=self.rules.homeboy_health[i-1]
                values[f'homeboy.level{i}.aim']=self.rules.homeboy_aim[i-1]
            for weapon in WEAPONS:values['weapon.price.'+weapon]=self.rules.weapon_prices[weapon]
            for name,value in values.items():
                var=ET.SubElement(out,'var');add(var,'name',name);add(var,'value',value)
            return out
        if c=='synctimestamps':
            db.execute('UPDATE users SET ack=stamp WHERE terminal=?',(u['terminal'],))
            out=ET.Element(c);add(out,'status','Ok');return out
        if c=='getuserinfo':
            if not u['registered']:raise GameError('Create your Colors player name','NoSuchUserException')
            return self.user_xml(u)
        if c=='setuserinfo':
            name=text(r,'colorsname',maxlen=64).strip()
            if not name:raise GameError('Empty user name')
            money=number(r,'money',u['money']) if self.rules.sync_single_player_money else u['money']
            # Membership is server-controlled; the client's gangid is informational.
            db.execute('UPDATE users SET name=?,money=?,registered=1,stamp=stamp+1 WHERE terminal=?',(name,money,u['terminal']))
            return self.bundle(db,c,u)
        if c=='getmapview':
            try:lat=float(text(r,'ypos'));lon=float(text(r,'xpos'))
            except ValueError as exc:raise GameError('Invalid GPS') from exc
            x,y,xoff,yoff=self.world.location(lat,lon)
            db.execute('UPDATE users SET lat=?,lon=?,position_time=? WHERE terminal=?',(lat,lon,self.clock(),u['terminal']))
            out=ET.Element(c);add(out,'xoffset',xoff);add(out,'yoffset',yoff)
            group=ET.SubElement(out,'turflist');gang_ids=set()
            for xy in self.world.coordinates(x,y):
                turf=self.turf(db,xy);group.append(self.turf_xml(db,turf))
                if turf['gang']:gang_ids.add(turf['gang'])
            gangs=ET.SubElement(out,'gangs')
            # The client lists nearby crews for joining, even on a free map.
            ids=gang_ids|({u['gang']} if u['gang'] else set())
            placeholders=','.join('?' for _ in ids)
            required=list(db.execute(f'SELECT * FROM gangs WHERE id IN ({placeholders}) ORDER BY id',tuple(ids))) if ids else []
            available=36-len(required)
            others=list(db.execute(f'SELECT * FROM gangs WHERE id NOT IN ({placeholders}) ORDER BY id LIMIT ?',(*ids,available))) if ids else list(db.execute('SELECT * FROM gangs ORDER BY id LIMIT 36'))
            for gang in required+others:gangs.append(self.gang_xml(gang))
            return out
        if c=='getganginfo':
            gid=number(r,'gangid',minimum=1)
            gang=db.execute('SELECT * FROM gangs WHERE id=?',(gid,)).fetchone()
            if gang is None:raise GameError('Crew not found','NoSuchGangException')
            out=self.gang_xml(gang);members=ET.SubElement(out,'gangmembers')
            for member in db.execute('SELECT * FROM users WHERE gang=? ORDER BY terminal',(gid,)):members.append(self.user_xml(member))
            return out
        if c=='getturflist':
            gid=number(r,'gangid')
            if gid!=u['gang'] or gid==0:raise GameError('Invalid crew')
            return self.bundle(db,c,u,all_turfs=True)
        if c in ('leavegang','dissolvegang','removeplayer','abdicate','releasegangmember','setganginfo'):
            return self.crew_action(db,c,r,u)
        if c=='sendsmsmessage':
            recipient=text(r,'recipienttermhwid',maxlen=128)
            if not db.execute('SELECT 1 FROM users WHERE terminal=?',(recipient,)).fetchone():raise GameError('Recipient not found')
            if db.execute('SELECT COUNT(*) FROM messages WHERE recipient=? AND delivered=0',(recipient,)).fetchone()[0]>=100:raise GameError('Inbox full')
            db.execute('INSERT INTO messages(recipient,sender,subject,body,created) VALUES(?,?,?,?,?)',
                       (recipient,u['name'],'Crew message',text(r,'message',maxlen=2048),self.clock()))
            out=ET.Element(c);add(out,'status','Ok');return out
        if c=='getmessages':
            out=ET.Element(c)
            messages=list(db.execute('SELECT * FROM messages WHERE recipient=? AND delivered=0 ORDER BY id LIMIT 100',(u['terminal'],)))
            for msg in messages:
                node=ET.SubElement(out,'message');stamp=time.gmtime(msg['created'])
                for key,value in [('text',msg['body']),('sender',msg['sender']),('subject',msg['subject']),
                                  ('logo',''),('date',time.strftime('%Y-%m-%d',stamp)),('time',time.strftime('%H:%M',stamp))]:add(node,key,value)
            db.executemany('UPDATE messages SET delivered=1 WHERE id=?',[(msg['id'],) for msg in messages])
            return out
        if c=='organizehomeboys':return self.organize(db,c,r,u)
        xy=self.client_pair(text(r,'turf')) if c!='setturfprice' else self.client_pair(f"{number(r,'xpos',minimum=1)},{number(r,'ypos',minimum=1)}")
        self.nearby(u,xy);turf=self.turf(db,xy)
        if c=='attackturf':return self.attack(db,c,r,u,xy,turf)
        self.unlocked(db,xy)
        if c=='buyturf':
            if turf['holder'] or turf['gang']:
                if turf['gang']==u['gang'] or turf['price']<=0:raise GameError('Turf is not for sale','TurfNorForSaleException')
                seller=db.execute('SELECT leader FROM gangs WHERE id=?',(turf['gang'],)).fetchone()
                cost=turf['price']
            else:seller=None;cost=self.rules.turf_price
            self.pay(db,u,cost)
            if seller:
                self.credit(db,seller['leader'],cost)
                self.notify(db,u,turf['gang'],'Turf purchased',f"{u['name']} purchased {xy[0]},{xy[1]}")
            db.execute("UPDATE turfs SET gang=?,holder=?,name=?,price=0,hb1=0,hb2=0,hb3=0,tag='',lastcollect=? WHERE x=? AND y=?",
                       (u['gang'],u['terminal'],text(r,'name','Unnamed',maxlen=64),self.clock(),*xy))
        elif c in ('creategang','joingang'):
            if u['gang']:raise GameError('Leave your current crew first','UserAlreadyGangmemberException')
            # Original menu sends creategang/joingang directly for the first
            # turf (e13cc -> e1424), without a preceding buyturf request.
            pending=turf['holder']==u['terminal'] and turf['gang']==0
            seller=None
            if not pending:
                if turf['holder'] or turf['gang']:
                    if not turf['gang'] or turf['price']<=0:raise GameError('Turf is not for sale','TurfNorForSaleException')
                    seller=db.execute('SELECT leader FROM gangs WHERE id=?',(turf['gang'],)).fetchone()
                    cost=turf['price']
                else:cost=self.rules.turf_price
                self.pay(db,u,cost)
                if seller:
                    self.credit(db,seller['leader'],cost)
                    self.notify(db,u,turf['gang'],'Turf purchased',f"{u['name']} purchased {xy[0]},{xy[1]}")
            if c=='creategang':
                g=r.find('gang')
                if g is None:raise GameError('Missing crew')
                name=text(g,'name',maxlen=64).strip();password=text(g,'password',maxlen=128)
                color=self.validate_color(text(g,'color'));tag=self.validate_tag(text(g,'tag','',maxlen=500000))
                if not name or not password:raise GameError('Crew name and password required')
                try:
                    gid=db.execute('INSERT INTO gangs(name,leader,color,tag,password) VALUES(?,?,?,?,?)',
                                   (name,u['terminal'],color,tag,password_hash(password))).lastrowid
                except sqlite3.IntegrityError as exc:raise GameError('Crew name already exists','DuplicateGangException') from exc
            else:
                gang=db.execute('SELECT * FROM gangs WHERE name=? COLLATE NOCASE',(text(r,'gangname',maxlen=64),)).fetchone()
                if not gang or not password_matches(text(r,'password',maxlen=128),gang['password']):raise GameError('Invalid crew password','PasswordMismatchException')
                gid=gang['id']
            db.execute('UPDATE users SET gang=? WHERE terminal=?',(gid,u['terminal']))
            db.execute("UPDATE turfs SET gang=?,holder=?,name=?,price=0,tag='',hb1=0,hb2=0,hb3=0,lastcollect=? WHERE x=? AND y=?",
                       (gid,u['terminal'],text(r,'turfname','Unnamed',maxlen=64),self.clock(),*xy))
            return self.bundle(db,c,u,xy,gang=True)
        elif c=='setturfprice':
            self.owned(u,turf)
            db.execute('UPDATE turfs SET price=? WHERE x=? AND y=?',(number(r,'price'),*xy))
        elif c=='setturfinfo':
            self.owned(u,turf)
            db.execute('UPDATE turfs SET name=? WHERE x=? AND y=?',(text(r,'name',maxlen=64),*xy))
        elif c=='tagturf':
            tag=self.validate_tag(text(r,'tag',maxlen=500000))
            db.execute('UPDATE turfs SET tag=? WHERE x=? AND y=?',(tag,*xy))
            self.notify(db,u,turf['gang'],'Turf tagged',f"{u['name']} tagged {xy[0]},{xy[1]}")
        elif c=='collectincome':
            self.owned(u,turf)
            periods=max(0,int((self.clock()-turf['lastcollect'])//self.rules.income_interval))
            income=min(self.rules.income_cap,periods*self.rules.daily_income)
            self.credit(db,u['terminal'],income)
            if periods:db.execute('UPDATE turfs SET lastcollect=? WHERE x=? AND y=?',(turf['lastcollect']+periods*self.rules.income_interval,*xy))
        elif c=='buyhomeboy':
            self.owned(u,turf)
            values=[0,0,0]
            for boy in r.findall('homeboys/homeboy'):
                level=number(boy,'type',minimum=1,maximum=3);values[level-1]+=number(boy,'number',maximum=100)
            for i,v in enumerate(values):
                if turf[f'hb{i+1}']+v>self.rules.homeboy_limits[i]:raise GameError('Homeboy limit exceeded')
            self.pay(db,u,sum(v*p for v,p in zip(values,self.rules.homeboy_prices)))
            db.execute('UPDATE turfs SET hb1=hb1+?,hb2=hb2+?,hb3=hb3+? WHERE x=? AND y=?',(*values,*xy))
        else:raise GameError('Unsupported command')
        return self.bundle(db,c,u,xy)

    @staticmethod
    def validate_color(value):
        try:components=[int(v) for v in value.split(',')]
        except ValueError as exc:raise GameError('Invalid crew color') from exc
        # Game represents channels as 16.16 values, not 8-bit RGB.
        if len(components)!=3 or any(not 0<=v<=65536 for v in components):raise GameError('Invalid crew color')
        return ','.join(map(str,components))

    @staticmethod
    def validate_tag(value):
        # Turf War tags are text in the manual, video and client serializer.
        # Preserve the payload; XML escaping is handled by ElementTree.
        if len(value)>500000:raise GameError('Tag too large')
        return value

    def crew_action(self,db,c,r,u):
        if c=='removeplayer':
            if u['gang']:
                gang=self.get_gang(db,u)
                if gang['leader']==u['terminal']:self.dissolve(db,gang['id'])
            db.execute('DELETE FROM attacks WHERE attacker=?',(u['terminal'],))
            db.execute("UPDATE turfs SET holder='' WHERE holder=?",(u['terminal'],))
            db.execute('DELETE FROM messages WHERE recipient=?',(u['terminal'],))
            db.execute('DELETE FROM users WHERE terminal=?',(u['terminal'],))
            out=ET.Element(c);add(out,'status','Ok');return out
        if c=='leavegang':
            gang=self.get_gang(db,u)
            if gang['leader']==u['terminal']:raise GameError('Transfer leadership or dissolve the crew first')
            db.execute('DELETE FROM attacks WHERE attacker=?',(u['terminal'],))
            db.execute('UPDATE users SET gang=0 WHERE terminal=?',(u['terminal'],))
            return self.bundle(db,c,u)
        gang=self.leader(db,u)
        if c=='dissolvegang':
            self.dissolve(db,gang['id']);return self.bundle(db,c,u)
        if c in ('abdicate','releasegangmember'):
            target=text(r,'newleadertermhwid' if c=='abdicate' else 'releaseehwid',maxlen=128)
            member=db.execute('SELECT * FROM users WHERE terminal=? AND gang=?',(target,gang['id'])).fetchone()
            if not member or target==u['terminal']:raise GameError('Invalid crew member')
            if c=='abdicate':db.execute('UPDATE gangs SET leader=? WHERE id=?',(target,gang['id']))
            else:
                db.execute('UPDATE users SET gang=0 WHERE terminal=?',(target,))
                db.execute('DELETE FROM attacks WHERE attacker=?',(target,))
        elif c=='setganginfo':
            g=r.find('gang')
            if g is None or number(g,'id')!=gang['id']:raise GameError('Invalid crew')
            if text(g,'gangleaderterminalhardwareid',gang['leader'])!=gang['leader']:raise GameError('Use abdicate to change leader')
            name=text(g,'name',maxlen=64).strip()
            if not name:raise GameError('Empty crew name')
            password=text(g,'password','__UNCHANGED__',maxlen=128)
            if not password:raise GameError('Empty password')
            hashed=gang['password'] if password=='__UNCHANGED__' else password_hash(password)
            try:
                db.execute('UPDATE gangs SET name=?,color=?,tag=?,password=? WHERE id=?',
                    (name,self.validate_color(text(g,'color')),self.validate_tag(text(g,'tag','',maxlen=500000)),hashed,gang['id']))
            except sqlite3.IntegrityError as exc:raise GameError('Crew name already exists','DuplicateGangException') from exc
        return self.bundle(db,c,u,gang=True)

    def organize(self,db,c,r,u):
        self.get_gang(db,u);changes={};totals=[0,0,0]
        for node in r.findall('turf'):
            xy=self.client_pair(text(node,'id'));turf=self.turf(db,xy)
            self.nearby_map(u,xy);self.owned(u,turf);self.unlocked(db,xy)
            changes.setdefault(xy,[0,0,0])
            for boy in node.findall('homeboys'):
                try:level=int(boy.attrib['type']);change=int(boy.attrib['change'])
                except (ValueError,KeyError) as exc:raise GameError('Invalid homeboy move') from exc
                if not 1<=level<=3 or abs(change)>100:raise GameError('Invalid homeboy move')
                changes[xy][level-1]+=change;totals[level-1]+=change
        if any(totals):raise GameError('Homeboy moves must conserve each level','HomeboysDoNotAddUpException')
        for xy,deltas in changes.items():
            turf=self.turf(db,xy)
            values=[turf[f'hb{i+1}']+delta for i,delta in enumerate(deltas)]
            if any(not 0<=v<=self.rules.homeboy_limits[i] for i,v in enumerate(values)):raise GameError('Invalid resulting homeboy count','HomeboyNumberLessThanZeroException')
            db.execute('UPDATE turfs SET hb1=?,hb2=?,hb3=? WHERE x=? AND y=?',(*values,*xy))
        out=self.bundle(db,c,u);group=ET.SubElement(out,'turflist')
        for xy in changes:group.append(self.turf_xml(db,self.turf(db,xy)))
        return out

    def nearby_map(self,u,xy):
        if u['position_time'] is None or self.clock()-u['position_time']>self.rules.position_age:raise GameError('Refresh map first')
        x,y,_,_=self.world.location(u['lat'],u['lon'])
        if self.world.xdistance(x,xy[0])>3 or abs(y-xy[1])>2:raise GameError('Turf outside visible map')

    def attack(self,db,c,r,u,xy,turf):
        gang=self.get_gang(db,u)
        action=r.attrib.get('action')
        current=db.execute('SELECT * FROM attacks WHERE x=? AND y=?',xy).fetchone()
        now=self.clock()
        if action=='initiate':
            if current:raise GameError('Turf already under attack')
            if not turf['gang'] or turf['gang']==u['gang']:raise GameError('Attack an enemy crew turf')
            if db.execute('SELECT 1 FROM attacks WHERE attacker=?',(u['terminal'],)).fetchone():raise GameError('One attack at a time')
            self.pay(db,u,self.rules.attack_price)
            db.execute('INSERT INTO attacks VALUES(?,?,?,?,?,?,?,?)',(*xy,u['terminal'],turf['gang'],'initiate',now,now+self.rules.attack_delay,now+self.rules.attack_expiry))
        else:
            if not current or current['attacker']!=u['terminal']:raise GameError('Attack not owned by this player')
            if action=='commence':
                if now<current['ready']:raise GameError('Attack countdown has not finished')
                db.execute("UPDATE attacks SET state='commence',expires=MAX(expires,?) WHERE x=? AND y=?",(now+self.rules.attack_expiry,*xy))
            elif action=='cancel':db.execute('DELETE FROM attacks WHERE x=? AND y=?',xy)
            elif action=='report':
                if current['state']!='commence':raise GameError('Attack has not commenced')
                outcome=r.find('outcome')
                if outcome is None or outcome.attrib.get('winner') not in ('attacker','owner'):raise GameError('Invalid battle outcome')
                lost=[0,0,0]
                for boy in outcome.findall('homeboyslost/homeboy'):
                    level=number(boy,'type',minimum=1,maximum=3);lost[level-1]+=number(boy,'number',maximum=100)
                if any(v>turf[f'hb{i+1}'] for i,v in enumerate(lost)):raise GameError('Invalid battle casualties')
                winner=outcome.attrib['winner']
                if winner=='attacker':
                    db.execute("UPDATE turfs SET gang=?,holder=?,tag='',price=0,hb1=0,hb2=0,hb3=0,lastcollect=? WHERE x=? AND y=?",(u['gang'],u['terminal'],now,*xy))
                else:db.execute('UPDATE turfs SET hb1=hb1-?,hb2=hb2-?,hb3=hb3-? WHERE x=? AND y=?',(*lost,*xy))
                self.notify(db,u,current['owner'],'Turf battle',f"{u['name']} attacked {xy[0]},{xy[1]}; winner: {winner}")
                db.execute('DELETE FROM attacks WHERE x=? AND y=?',xy)
            else:raise GameError('Invalid attack action')
        out=self.bundle(db,c,u,xy)
        if action=='report':
            group=ET.SubElement(out,'turflist');group.append(self.turf_xml(db,self.turf(db,xy)))
        return out
