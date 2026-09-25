"""Extract CSV and documentation only; preserve source archive and exclude emulator tar."""
import argparse
from pathlib import Path
from zipfile import ZipFile

parser=argparse.ArgumentParser();parser.add_argument('archive');parser.add_argument('--out',default='dataset');args=parser.parse_args()
root=Path(args.out).resolve();root.mkdir(parents=True,exist_ok=True)
with ZipFile(args.archive) as z:
    for entry in z.infolist():
        if entry.is_dir() or not entry.filename.endswith(('.csv','.md')):continue
        target=(root/entry.filename).resolve()
        if root not in target.parents:raise ValueError('Unsafe archive path')
        if entry.file_size>200_000_000:raise ValueError('Unexpectedly large file')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(entry))
print('Data extracted to',root)
