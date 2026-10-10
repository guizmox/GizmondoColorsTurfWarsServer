"""Isolated ARM compatibility test runtime, never a production server dependency.
Executes the original game algorithms; only WinCE CRT imports and calls to
unrelated game/UI state are adapted. These adaptations are enumerated below.
"""
import sys,struct,json,re,hashlib
from pathlib import Path
import pefile
from unicorn import *
from unicorn.arm_const import *
raw=Path(sys.argv[1]).read_bytes()
assert hashlib.sha256(raw).hexdigest()=='addaca723aba2dd4beb7449bbbd37a89988cb0345ee5381e08f2c18dc62473cf', 'Unsupported executable version'
p=pefile.PE(data=raw); uc=Uc(UC_ARCH_ARM,UC_MODE_ARM); mapped=p.get_memory_mapped_image();uc.mem_map(0x10000,(len(mapped)+4095)&~4095);uc.mem_write(0x10000,mapped)
uc.mem_map(0x2000000,0x1800000); heap=0x2100000
ordinals=json.loads((Path(__file__).with_name('wince-ordinals.json')).read_text()); imports={}
for d in p.DIRECTORY_ENTRY_IMPORT:
 for i in d.imports:
  a=0x2000000+len(imports)*4;imports[a]=i.name.decode() if i.name else ordinals.get(str(i.ordinal),f'ord{i.ordinal}');uc.mem_write(i.address,struct.pack('<I',a))
def malloc(n):
 global heap
 a=heap;heap+=(max(n,1)+15)&~15;assert heap<0x3600000;return a
def cstr(a):
 out=bytearray()
 if not a:return b''
 for n in range(2000000):
  b=uc.mem_read(a+n,1)[0]
  if not b:return bytes(out)
  out.append(b)
 raise ValueError('unterminated')
def write(a,v):uc.mem_write(a,v)
def hook(u,a,size,_):
 if a==0x18060:
  u.reg_write(UC_ARM_REG_R0,0);u.reg_write(UC_ARM_REG_PC,u.reg_read(UC_ARM_REG_LR));return
 if a==0x8ff70:
  u.reg_write(UC_ARM_REG_R0,0x2070000);u.reg_write(UC_ARM_REG_PC,u.reg_read(UC_ARM_REG_LR));return
 if a not in imports:return
 name=imports[a];args=[u.reg_read(r) for r in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)];x,y,z,w=args;ret=0;hi=None
 if name=='malloc' or name.startswith('??2@') or name.startswith('??_U@'):ret=malloc(x)
 elif name=='free' or name.startswith('??3@') or name.startswith('??_V@'):pass
 elif name=='calloc':ret=malloc(x*y)
 elif name=='realloc':ret=malloc(y);write(ret,bytes(u.mem_read(x,min(y,1024))))
 elif name in ('memcpy','memmove'):write(x,bytes(u.mem_read(y,z)));ret=x
 elif name=='memset':write(x,bytes([y&255])*z);ret=x
 elif name=='strlen':ret=len(cstr(x))
 elif name=='strcpy':write(x,cstr(y)+b'\0');ret=x
 elif name=='strcat':write(x+len(cstr(x)),cstr(y)+b'\0');ret=x
 elif name=='strncpy':write(x,(cstr(y)[:z]+b'\0'*z)[:z]);ret=x
 elif name in ('strcmp','strncmp','_stricmp','_strnicmp'):
  a1=cstr(x);a2=cstr(y)
  if 'icmp' in name or 'nicmp' in name:a1=a1.lower();a2=a2.lower()
  if name in ('strncmp','_strnicmp'):a1=a1[:z];a2=a2[:z]
  ret=(a1>a2)-(a1<a2)
 elif name=='memcmp':a1=bytes(u.mem_read(x,z));a2=bytes(u.mem_read(y,z));ret=(a1>a2)-(a1<a2)
 elif name in ('strchr','strrchr'):
  s=cstr(x)+b'\0';k=s.find(bytes([y&255])) if name=='strchr' else s.rfind(bytes([y&255]));ret=x+k if k>=0 else 0
 elif name=='strstr':k=cstr(x).find(cstr(y));ret=x+k if k>=0 else 0
 elif name in ('atoi','atol','_atoi64'):
  m=re.match(rb'\s*([+-]?\d+)',cstr(x));v=int(m[1]) if m else 0;ret=v&0xffffffff
  if name=='_atoi64':hi=(v>>32)&0xffffffff
 elif name in ('__rt_sdiv','__rt_udiv'):
  if name=='__rt_sdiv':x=x-(1<<32) if x>>31 else x;y=y-(1<<32) if y>>31 else y
  ret=(abs(y)//abs(x))*(-1 if (x<0)!=(y<0) else 1);hi=(y-ret*x)&0xffffffff
 elif name=='__rt_sdiv64by64':
  n=x|(y<<32);d=z|(w<<32);n=n-(1<<64) if n>>63 else n;d=d-(1<<64) if d>>63 else d;v=(abs(n)//abs(d))*(-1 if (n<0) !=(d<0) else 1);ret=v&0xffffffff;hi=v>>32&0xffffffff
 elif name in ('vsprintf','sprintf'):
  fmt=cstr(y).decode(); vals=[];ptr=z if name=='vsprintf' else None
  specs=list(re.finditer(r'%(?:[-+0 #]*\d*(?:\.\d+)?[hlL]*)([dsuxXc%])',fmt))
  index=0
  for m in specs:
   if m[1]=='%':continue
   v=struct.unpack('<I',u.mem_read(ptr+index*4,4))[0] if ptr else args[index+2]
   index+=1
   vals.append(cstr(v).decode() if m[1]=='s' else (v-(1<<32) if m[1]=='d' and v>>31 else v))
  out=(fmt.replace('%u','%d')%tuple(vals)).encode();write(x,out+b'\0');ret=len(out)
 elif name=='fopen':ret=0
 elif name=='_isctype':
  c=chr(x) if x<256 else '';mask=(1 if c.isupper() else 0)|(2 if c.islower() else 0)|(4 if c.isdigit() else 0)|(8 if c in ' \t\n\r\v\f' else 0)|(16 if c and not c.isalnum() and not c.isspace() else 0)|(32 if x<32 or x==127 else 0)|(64 if c in ' \t' else 0)|(128 if c in '0123456789abcdefABCDEF' else 0);ret=mask&y
 else:raise RuntimeError((name,list(map(hex,args)),hex(u.reg_read(UC_ARM_REG_LR))))
 u.reg_write(UC_ARM_REG_R0,ret&0xffffffff)
 if hi is not None:u.reg_write(UC_ARM_REG_R1,hi)
 u.reg_write(UC_ARM_REG_PC,u.reg_read(UC_ARM_REG_LR))
uc.hook_add(UC_HOOK_CODE,hook,begin=0x2000000,end=0x2001000)
for address in (0x18060,0x8ff70): uc.hook_add(UC_HOOK_CODE,hook,begin=address,end=address)
def call(a,*args):
 for r,v in zip((UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3),args):uc.reg_write(r,v)
 uc.reg_write(UC_ARM_REG_SP,0x201f000);uc.reg_write(UC_ARM_REG_LR,0x201ff00)
 try:uc.emu_start(a,0x201ff00,count=100000000)
 except Exception:print('PC',hex(uc.reg_read(UC_ARM_REG_PC)));raise
 assert uc.reg_read(UC_ARM_REG_PC)==0x201ff00,f'instruction limit PC={uc.reg_read(UC_ARM_REG_PC):x}';return uc.reg_read(UC_ARM_REG_R0)
def parse(xml):
 node=malloc(0x38);call(0xb2774,node);stream=malloc(0x20);call(0xb7c48,stream,node);string=malloc(8);data=malloc(len(xml)+1);write(data,xml+b'\0');call(0xadf9c,string,data);call(0xb855c,stream,string);return node
