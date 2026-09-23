#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('guard',Path(__file__).with_name('guard-qemu-numad.py'))
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

class IdentityTests(unittest.TestCase):
    def test_only_named_qemu_and_matching_socket_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'guest-tools-final';run.mkdir();(run/'qemu.pid').write_text('42')
            proc=root/'proc'/'42';proc.mkdir(parents=True);(proc/'exe').symlink_to(root/'qemu-system-x86_64')
            (proc/'stat').write_text('42 (qemu-system) '+' '.join(['S']+['0']*18+['100']))
            cmd=['qemu-system-x86_64','-name',run.name,'-qmp','unix:'+str(run/'qmp.sock')]
            def write(args): (proc/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
            write(cmd);self.assertEqual(guard.identity(run,root/'proc'),(42,'100'))
            write([*cmd[:2],'another-vm',*cmd[3:]])
            self.assertIsNone(guard.identity(run,root/'proc'))
            write([*cmd[:-1],'unix:/other/qmp.sock'])
            self.assertIsNone(guard.identity(run,root/'proc'))
            write(cmd);(proc/'exe').unlink();(proc/'exe').symlink_to(root/'python3')
            self.assertIsNone(guard.identity(run,root/'proc'))
            (run/'qemu.pid').write_text('43');self.assertIsNone(guard.identity(run,root/'proc'))

if __name__=='__main__':unittest.main()
