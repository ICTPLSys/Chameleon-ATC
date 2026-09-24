#!/usr/bin/env python3
"""Installer boundary tests; never install a kernel on the running machine."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[1] / "scripts/kernel-deploy.py"
spec = importlib.util.spec_from_file_location("kernel_deploy", script)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class InstallDestinationTests(unittest.TestCase):
    def arguments(self, package, destination):
        return SimpleNamespace(package=package, system=False, destdir=destination,
                               output=package.parent, role="guest")

    def test_real_root_requires_system(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.arguments(Path(temp) / "package", Path("/"))
            with patch.object(deploy, "run") as execute:
                with self.assertRaisesRegex(RuntimeError, "Use --system"):
                    deploy.install(args)
                execute.assert_not_called()

    def test_root_symlink_requires_system(self):
        with tempfile.TemporaryDirectory() as temp:
            root_link = Path(temp) / "root-link"
            root_link.symlink_to("/", target_is_directory=True)
            args = self.arguments(Path(temp) / "package", root_link)
            with self.assertRaisesRegex(RuntimeError, "Use --system"):
                deploy.install(args)

    def test_package_cannot_be_its_own_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            for destination in (package, package / "boot/staging"):
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(RuntimeError, "outside the package"):
                        deploy.install(self.arguments(package, destination))


if __name__ == "__main__":
    unittest.main()
