"""File barrier for independently prepared application VMs."""
import json
import math
import os
from pathlib import Path
import re
import time


def wait(directory, member, timeout, evidence=None):
    directory = Path(directory)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,70}', member):
        raise ValueError('Invalid barrier member')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Barrier timeout must be finite and positive')
    if not directory.is_dir():
        raise ValueError('Parent must create a fresh barrier directory')
    if (directory / 'ABORT').exists():
        raise RuntimeError('Co-run barrier aborted: ' + (directory / 'ABORT').read_text()[:1000])
    if (directory / 'RELEASE').exists():
        raise RuntimeError('Barrier was released before this member became ready')
    ready = {'member': member, 'pid': os.getpid(), 'ready_unix_seconds': time.time(),
             'evidence': evidence or {}}
    path = directory / (member + '.ready.json')
    # Publish complete JSON atomically and reject reused member identities.
    temporary = directory / ('.' + member + '.' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(ready, stream)
            stream.write('\n')
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        if (directory / 'ABORT').exists():
            raise RuntimeError('Co-run barrier aborted: ' + (directory / 'ABORT').read_text()[:1000])
        if (directory / 'RELEASE').exists():
            return {**ready, 'released_unix_seconds': time.time(),
                    'release_file': str(directory / 'RELEASE')}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Co-run start barrier timed out for ' + member)
        time.sleep(min(.1, remaining))


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--member',required=True)
    parser.add_argument('--timeout',type=float,default=1800)
    parser.add_argument('--case',required=True)
    parser.add_argument('--vm',required=True)
    parser.add_argument('--phase',required=True)
    args=parser.parse_args()
    result=wait(args.directory,args.member,args.timeout,
                {'case':args.case,'vm':args.vm,'phase':args.phase})
    print(json.dumps(result))


if __name__=='__main__':
    main()
