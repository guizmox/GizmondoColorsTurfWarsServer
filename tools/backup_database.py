"""Consistent online SQLite backup on a local filesystem."""
import argparse
import sqlite3
from pathlib import Path
import os
import tempfile

p=argparse.ArgumentParser()
p.add_argument('database',type=Path)
p.add_argument('destination',type=Path)
a=p.parse_args()
a.destination.parent.mkdir(parents=True,exist_ok=True)
fd,name=tempfile.mkstemp(prefix='.backup-',dir=a.destination.parent);os.close(fd)
try:
    src=sqlite3.connect(a.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=15)
    dst=sqlite3.connect(name)
    try:
        src.backup(dst,pages=256,sleep=0.1)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Backup integrity check failed')
    finally:
        dst.close();src.close()
    os.replace(name,a.destination)
finally:
    Path(name).unlink(missing_ok=True)
