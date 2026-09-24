#!/usr/bin/env python3
"""Build, package, and install independent disk-boot Chameleon kernels.

This does not change build/guest or build/host. Installation into the running
machine requires --system; otherwise --destdir creates an offline staging tree.
A package includes the Guest Hermit module built against precisely that kernel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def config_values(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
    return values


def validate_config(path, role):
    values = config_values(path)
    required = {
        "MODULES": "y", "MODULE_UNLOAD": "y", "MODVERSIONS": "n",
        "MLX5_CORE": "m", "MLX5_CORE_EN": "y", "MLX5_CORE_IPOIB": "y",
        "INFINIBAND": "m", "MLX5_INFINIBAND": "m", "INFINIBAND_IPOIB": "m",
        "INFINIBAND_USER_ACCESS": "m", "INFINIBAND_ADDR_TRANS": "y",
        "VIRTIO_BLK": "y", "SCSI_VIRTIO": "y", "EXT4_FS": "y",
        "TRANSPARENT_HUGEPAGE": "y", "PSI": "y", "PCI_IOV": "y", "GUP_TEST": "y",
        "PROC_PAGE_MONITOR": "y", "DEBUG_FS": "y",
    }
    if role == "guest":
        required.update(LLFREE="y", CHAMELEON="y", CHAMELEON_POLICY="y", CHAMELEON_TEST="y",
                        VIRTIO_LLFREE_BALLOON="y", MEMCG="n", LRU_GEN="n",
                        MEMORY_HOTPLUG="n", CMA="n", NUMA="n",
                        CHECKPOINT_RESTORE="y", MEM_SOFT_DIRTY="y", IKCONFIG_PROC="y",
                        DEBUG_VM="y", DEBUG_LIST="y")
    else:
        required.update(LLFREE="n", KVM="m", KVM_INTEL="m", VFIO_PCI="m",
                        VFIO_IOMMU_TYPE1="m", INTEL_IOMMU="y", IRQ_REMAP="y", AMD_IOMMU="y",
                        VFIO_NOIOMMU="n", MLX5_ESWITCH="y")
    wrong = [f"CONFIG_{key}: {values.get('CONFIG_' + key, 'n')} != {want}"
             for key, want in required.items()
             if values.get("CONFIG_" + key, "n") != want]
    if wrong:
        raise RuntimeError("Deployment config lost required options:\n" + "\n".join(wrong))


def make_command(args):
    command = ["make", "-C", ROOT / "linux", f"O={args.output}",
               f"CC={args.cc}", "LOCALVERSION="]
    return command


def configure(args):
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = args.output / ".config"
    if args.base_config:
        shutil.copyfile(args.base_config, cfg)
    else:
        run(make_command(args) + ["x86_64_defconfig"])
    fragments = [ROOT / "configs/deploy-common.fragment",
                 ROOT / f"configs/deploy-{args.role}.fragment"]
    run([ROOT / "linux/scripts/kconfig/merge_config.sh", "-m", "-O",
         args.output, cfg, *fragments])
    run(make_command(args) + ["olddefconfig"])
    validate_config(cfg, args.role)
    metadata = {"role": args.role, "base_config": str(args.base_config) if args.base_config else "x86_64_defconfig",
                "fragments": {str(path.relative_to(ROOT)): digest(path) for path in fragments}}
    (args.output / "deploy-config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Deployment {args.role} config: {cfg}")


def build(args):
    if args.base_config or not (args.output / ".config").exists():
        configure(args)
    validate_config(args.output / ".config", args.role)
    run(make_command(args) + [f"-j{args.jobs}", "bzImage", "modules"])
    if args.role == "guest":
        module = args.output / "hermit-client"
        module.mkdir(exist_ok=True)
        # Preserve the acceptance module in hermit/client. Build artifacts are
        # separate even if tests and a deployment build execute concurrently.
        for name in ("Makefile", "rswap_client.c", "rswap_rdma.c", "rswap_transport.h"):
            shutil.copyfile(ROOT / "hermit/client" / name, module / name)
        shutil.copyfile(ROOT / "hermit/wire.h", args.output / "wire.h")
        run(make_command(args) + [f"-j{args.jobs}", f"M={module}", "modules"])
    print(f"Deployment {args.role} image: {args.output / 'arch/x86/boot/bzImage'}")


def package(args):
    validate_config(args.output / ".config", args.role)
    release = (args.output / "include/config/kernel.release").read_text().strip()
    expected = "6.18.0-chameleon-" + args.role
    if release != expected:
        raise RuntimeError(f"Expected release {expected}, got {release}")
    needed = [args.output / name for name in
              ("arch/x86/boot/bzImage", "System.map", "modules.order", "Module.symvers")]
    if args.role == "guest":
        needed.append(args.output / "hermit-client/rswap-client.ko")
    for path in needed:
        if not path.is_file():
            raise RuntimeError(f"Build the deployment kernel first: missing {path}")
    pkg = args.package
    pkg.mkdir(parents=True, exist_ok=True)
    # A fixed release replaces only its own staging files, never unrelated kernels.
    module_dir = pkg / "lib/modules" / release
    if module_dir.exists():
        shutil.rmtree(module_dir)
    run(make_command(args) + ["INSTALL_MOD_STRIP=1", f"INSTALL_MOD_PATH={pkg}",
                             "DEPMOD=true", "modules_install"])
    for name in ("build", "source"):
        link = module_dir / name
        if link.is_symlink():
            link.unlink()
    if args.role == "guest":
        extra = module_dir / "extra"
        extra.mkdir(exist_ok=True)
        shutil.copyfile(needed[-1], extra / "rswap-client.ko")
    boot = pkg / "boot"
    boot.mkdir(exist_ok=True)
    for source, name in ((needed[0], "vmlinuz"), (needed[1], "System.map"),
                         (args.output / ".config", "config")):
        shutil.copyfile(source, boot / f"{name}-{release}")
    run(["depmod", "-b", pkg, "-F", boot / f"System.map-{release}", release])
    modpaths = [str(path.relative_to(pkg)) for path in module_dir.rglob("*.ko*")]
    metadata_file = args.output / "deploy-config.json"
    configuration = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    manifest = {"role": args.role, "kernelrelease": release,
                "compiler": config_values(args.output / ".config").get("CONFIG_CC_VERSION_TEXT", args.cc),
                "configuration": configuration,
                "modules": len(modpaths), "files": {}}
    for path in sorted(pkg.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name not in ("manifest.json", "install-kernel.py"):
            manifest["files"][str(path.relative_to(pkg))] = digest(path)
    (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(Path(__file__), pkg / "install-kernel.py")
    archive = args.output / f"{release}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for path in sorted(pkg.iterdir()):
            stream.add(path, arcname=path.name)
    print(f"Package directory: {pkg}\nTransfer archive: {archive}\n"
          "On the target, extract and run:\n"
          f"  sudo python3 install-kernel.py install --role {args.role} --package . --system --update-grub")


def test_modules(args):
    if args.role != "guest":
        raise RuntimeError("test-modules builds the Guest acceptance fixtures only")
    if not (args.output / "Module.symvers").is_file():
        raise RuntimeError("Build the deployment Guest first")
    target = args.output / "test-modules"
    target.mkdir(parents=True, exist_ok=True)
    names = ("guest_allocator", "chameleon_shadow_backend", "chameleon_psi_load",
             "chameleon_control_cleanup")
    for name in names:
        shutil.copyfile(ROOT / "tests" / (name + ".c"), target / (name + ".c"))
    (target / "Makefile").write_text("obj-m += " + " ".join(name + ".o" for name in names) + "\n")
    run(make_command(args) + [f"-j{args.jobs}", f"M={target}", "modules"])
    print(f"Deployment Guest acceptance modules: {target}")


def install(args):
    pkg = args.package
    destination = Path("/") if args.system else (args.destdir or args.output / "staging").resolve()
    if not args.system and destination == Path("/"):
        raise RuntimeError("Use --system for an installation to /")
    if destination == pkg or pkg in destination.parents:
        raise RuntimeError("Install destination must be outside the package directory")
    manifest = json.loads((pkg / "manifest.json").read_text())
    release = manifest["kernelrelease"]
    if manifest["role"] != args.role or release != "6.18.0-chameleon-" + args.role:
        raise RuntimeError("Package role/release does not match --role")
    for name, expected in manifest["files"].items():
        path = pkg / name
        if pkg not in path.parents or ".." in Path(name).parts or not path.is_file():
            raise RuntimeError(f"Invalid or missing package member: {name}")
        if digest(path) != expected:
            raise RuntimeError(f"Package file changed: {name}")
    if args.system:
        if os.geteuid() != 0:
            raise RuntimeError("--system requires root; use --destdir for offline installation")
        if os.uname().release == release:
            raise RuntimeError("Refusing to overwrite the running kernel's modules; boot another kernel first")
        if not shutil.which("update-initramfs") and not shutil.which("dracut"):
            raise RuntimeError("Install initramfs-tools or dracut before --system installation")
    else:
        destination.mkdir(parents=True, exist_ok=True)
    for directory in ("boot", "lib/modules"):
        shutil.copytree(pkg / directory, destination / directory, dirs_exist_ok=True, symlinks=True)
    run(["depmod", "-b", destination, "-F", destination / f"boot/System.map-{release}", release])
    if args.system:
        if shutil.which("update-initramfs"):
            mode = "-u" if (destination / f"boot/initrd.img-{release}").exists() else "-c"
            run(["update-initramfs", mode, "-k", release])
        else:
            run(["dracut", "--force", f"/boot/initramfs-{release}.img", release])
        if args.update_grub:
            if not shutil.which("update-grub"):
                raise RuntimeError("Kernel installed; update-grub unavailable. Update this machine's bootloader manually.")
            run(["update-grub"])
        print(f"Installed {release}. Select it in the bootloader and reboot when ready.")
    else:
        print(f"Offline install: {destination}; {release}. Target initramfs/bootloader unchanged.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "build", "package", "install", "test-modules"))
    parser.add_argument("--role", choices=("host", "guest"), required=True)
    parser.add_argument("--output", type=Path, help="default: build/deploy-ROLE")
    parser.add_argument("--base-config", type=Path, help="optional machine/distro config, e.g. /boot/config-$(uname -r)")
    parser.add_argument("--cc", default="gcc",
                        help="kernel/module compiler; default gcc (some OpenCilk clang builds crash in mlx5)")
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--package", type=Path, help="package directory, default: OUTPUT/package")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--destdir", type=Path, help="offline install root, default: OUTPUT/staging")
    location.add_argument("--system", action="store_true", help="install to this machine's /boot and /lib/modules; create initramfs")
    parser.add_argument("--update-grub", action="store_true", help="with install --system only; never selects a default kernel or reboots")
    args = parser.parse_args()
    args.output = (args.output or ROOT / f"build/deploy-{args.role}").resolve()
    args.package = (args.package or args.output / "package").resolve()
    if args.package == Path("/"):
        parser.error("Package directory cannot be /; use install --system for system installation")
    if args.destdir:
        args.destdir = args.destdir.resolve()
        if args.destdir == Path("/"):
            parser.error("Use --system for an installation to /")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.update_grub and not (args.action == "install" and args.system):
        parser.error("--update-grub requires install --system")
    if (args.system or args.destdir) and args.action != "install":
        parser.error("--system/--destdir apply only to install")
    try:
        {"configure": configure, "build": build, "package": package, "install": install,
         "test-modules": test_modules}[args.action](args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"kernel-deploy: {error}\n")


if __name__ == "__main__":
    main()
