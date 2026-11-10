"""Remote ECU tuning over USB/IP (docs/operations/remote-ecu.md).

The script runs as root on both hosts against live sysfs and the usbip CLI,
so these tests drive it against a fake sysfs tree and a stub `usbip` on PATH.
The rest pins the properties the runbook's safety section relies on: the
vehicle export cannot be enabled at boot, usbipd dies with it, the Windows
guest never starts with a plain `up -d`, the guest gets the ECU as a usbredir
stream rather than a QEMU usb-host device, and every piece reads the same IDs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "ecu_usbip.sh"
SYSTEMD = ROOT / "deploy" / "systemd"
VID, PID = "1a2b", "3c4d"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _device(root: Path, parent: str, busid: str, vid: str, pid: str, driver: str | None) -> None:
    """One USB device: the real node under devices/, the bus/usb/devices link to it."""
    node = root / "devices" / parent / busid
    node.mkdir(parents=True)
    (node / "idVendor").write_text(vid + "\n")
    (node / "idProduct").write_text(pid + "\n")
    if driver:
        drv = root / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (node / "driver").symlink_to(drv)
    links = root / "bus"
    links.mkdir(exist_ok=True)
    (links / busid).symlink_to(node)


def _stub_vm(tmp_path: Path, *, running: bool = True, nat_exempt: bool = True) -> None:
    """A `docker` whose guest is (not) running and whose QEMU monitor answers
    nothing (so every `info` check misses and every add is attempted), plus a
    `usbredirect` that returns at once. Both record what they were asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {tmp_path / "docker.calls"}\n'
        'case "$1" in\n'
        f'inspect) if [[ "$*" == *Running* ]]; then echo {"true" if running else "false"}; '
        "else echo 172.19.0.2; fi ;;\n"
        'exec) if [[ "$*" == *iptables* ]]; then '
        f'[[ "$*" == *" -C "* ]] && exit {0 if nat_exempt else 1}; exit 0; fi\n'
        f"        cat >> {tmp_path / 'monitor.in'} ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    redirect = bin_dir / "usbredirect"
    redirect.write_text(f'#!/usr/bin/env bash\necho "$*" >> {tmp_path / "usbredirect.calls"}\n')
    redirect.chmod(0o755)


def _run(tmp_path: Path, *args: str, usbip: str | None = None, **env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "usbip.calls"
    if usbip is not None:
        stub = bin_dir / "usbip"
        stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\ncase "$1" in\n{usbip}\nesac\n')
        stub.chmod(0o755)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OPENLAPS_ECU_CONFIG": str(tmp_path / "absent.env"),
        "OPENLAPS_ECU_SYSFS": str(tmp_path / "sys" / "bus"),
        "OPENLAPS_ECU_USB_VID": VID,
        "OPENLAPS_ECU_USB_PID": PID,
        **env,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], env=full_env, capture_output=True, text=True, timeout=10
    )
    made = calls.read_text().splitlines() if calls.exists() else []
    return proc, made


def test_status_tells_a_local_ecu_from_the_pits_vhci_copy(tmp_path):
    sys_root = tmp_path / "sys"
    _device(sys_root, "platform/xhci/usb1", "1-1.2", VID, PID, "usbip-host")
    _device(sys_root, "platform/xhci/usb1", "1-1.3", "dead", "beef", "uvcvideo")
    _device(sys_root, "platform/vhci_hcd.0/usb3", "3-1", VID, PID, None)

    proc, _ = _run(tmp_path, "status")

    assert proc.returncode == 0, proc.stderr
    assert "1-1.2 (exported via usbip-host)" in proc.stdout
    assert "3-1" not in proc.stdout.split("attached here")[0], "a vhci port is not a local ECU"
    assert "1-1.3" not in proc.stdout, "another device on the bus is ignored"
    assert "attached here over vhci: yes" in proc.stdout


def test_an_unbound_ecu_is_reported_as_held_by_its_local_driver(tmp_path):
    # sysfs IDs are lowercase; the operator may type them either way.
    _device(tmp_path / "sys", "platform/xhci/usb1", "1-1", VID, PID, "cdc_acm")
    proc, _ = _run(tmp_path, "status", OPENLAPS_ECU_USB_VID=VID.upper())
    assert "1-1 (local driver)" in proc.stdout
    assert "attached here over vhci: no" in proc.stdout


def test_remote_bus_id_is_found_by_vid_pid_not_by_position(tmp_path):
    (tmp_path / "sys" / "bus").mkdir(parents=True)
    listing = (
        "list) cat <<EOF\nExportable USB devices\n======================\n"
        " - 192.168.12.222\n"
        "      1-1.1: Some Camera : unknown product (dead:beef)\n"
        f"      1-1.2: unknown vendor : unknown product ({VID}:{PID})\n"
        "EOF\n;;"
    )
    proc, calls = _run(tmp_path, "status", usbip=listing, OPENLAPS_ECU_HOST="192.168.12.222")
    assert proc.returncode == 0, proc.stderr
    assert "exported by 192.168.12.222: 1-1.2" in proc.stdout
    assert calls == ["list -r 192.168.12.222"]


def test_detach_drops_only_the_ports_carrying_the_ecu(tmp_path):
    (tmp_path / "sys" / "bus").mkdir(parents=True)
    ports = (
        "port) cat <<EOF\nImported USB devices\n====================\n"
        "Port 00: <Port in Use> at Full Speed(12Mbps)\n"
        "       unknown vendor : unknown product (dead:beef)\n"
        "Port 01: <Port in Use> at Full Speed(12Mbps)\n"
        f"       unknown vendor : unknown product ({VID.upper()}:{PID})\n"
        "       3-2 -> usbip://192.168.12.222:3240/1-1.2\n"
        "EOF\n;;"
    )
    proc, calls = _run(tmp_path, "detach", usbip=ports)
    assert proc.returncode == 0, proc.stderr
    assert calls == ["port", "detach -p 1"]


def test_ids_that_are_not_four_hex_digits_are_refused(tmp_path):
    proc, _ = _run(tmp_path, "status", OPENLAPS_ECU_USB_VID="0x1a2b")
    assert proc.returncode == 2
    assert "4 hex digits" in proc.stderr


def test_the_config_file_fills_only_what_the_environment_leaves_unset(tmp_path):
    (tmp_path / "sys" / "bus").mkdir(parents=True)
    cfg = tmp_path / "openlaps-ecu"
    cfg.write_text(
        f"# comment\nOPENLAPS_ECU_USB_VID={VID}\nOPENLAPS_ECU_USB_PID={PID}  # trailing\n"
        "OPENLAPS_ECU_HOST=192.168.12.222\nPATH=/nowhere\n"
    )
    listing = f'list) echo "      1-4: x : y ({VID}:{PID})";;'
    proc, calls = _run(
        tmp_path,
        "status",
        usbip=listing,
        OPENLAPS_ECU_CONFIG=str(cfg),
        OPENLAPS_ECU_USB_VID="",
        OPENLAPS_ECU_USB_PID="",
        OPENLAPS_ECU_HOST="10.0.0.9",
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == ["list -r 10.0.0.9"], "the environment's host wins over the file's"
    assert "exported by 10.0.0.9: 1-4" in proc.stdout


# --- deploy files -------------------------------------------------------------


def test_remote_access_cannot_be_left_armed_across_a_reboot():
    export = (SYSTEMD / "openlaps-ecu-export.service").read_text()
    usbipd = (SYSTEMD / "openlaps-usbipd.service").read_text()

    def sections(unit: str) -> list[str]:
        return [line.strip() for line in unit.splitlines() if line.startswith("[")]

    assert "[Install]" not in sections(export), "the export is armed per session, never enabled"
    assert "[Install]" not in sections(usbipd)
    assert "PartOf=openlaps-ecu-export.service" in usbipd, "disarming stops usbipd too"
    assert "ExecStopPost=/usr/local/sbin/openlaps-ecu-usbip unexport" in export


def test_every_unit_reads_the_file_the_script_defaults_to():
    default = re.search(r"OPENLAPS_ECU_CONFIG:-([^}]+)\}", SCRIPT.read_text()).group(1)
    for name in (
        "openlaps-ecu-export.service",
        "openlaps-ecu-attach.service",
        "openlaps-ecu-redirect.service",
    ):
        assert f"EnvironmentFile={default}" in (SYSTEMD / name).read_text(), name


def test_the_windows_guest_is_opt_in_and_takes_the_ecu_as_a_stream():
    compose = yaml.safe_load((ROOT / "deploy" / "pit-compose.yaml").read_text())
    svc = compose["services"]["ecu-windows"]
    assert svc["profiles"] == ["ecu"], "a plain `up -d` must not boot a Windows VM"
    # usb-host in the guest cannot follow an ECU reset (libusb in the container
    # never sees hotplug), so the guest gets a usb-redir device from the
    # redirect loop instead and needs no USB access of its own.
    assert "usb-host" not in svc["environment"].get("ARGUMENTS", "")
    assert not any("/dev/bus/usb" in v for v in svc["volumes"])
    assert not any("/dev/bus/usb" in d for d in svc["devices"])
    assert svc["container_name"] == "ecu-windows", "OPENLAPS_ECU_VM names the container"


def test_redirect_is_safe_to_enable_and_cleans_the_guest_up_on_stop():
    unit = (SYSTEMD / "openlaps-ecu-redirect.service").read_text()
    assert "[Install]" in unit and "WantedBy=multi-user.target" in unit
    assert "ExecStart=/usr/local/sbin/openlaps-ecu-usbip redirect" in unit
    assert "ExecStopPost=/usr/local/sbin/openlaps-ecu-usbip unredirect" in unit
    assert "After=docker.service openlaps-ecu-attach.service" in unit


def test_redirect_streams_the_attached_ecu_into_the_guest(tmp_path):
    root = tmp_path / "sys"
    _device(root, "platform/vhci_hcd.0/usb5", "5-1", VID, PID, "ftdi_sio")
    (root / "devices/platform/vhci_hcd.0/usb5/5-1/busnum").write_text("5\n")
    (root / "devices/platform/vhci_hcd.0/usb5/5-1/devnum").write_text("7\n")
    _stub_vm(tmp_path, nat_exempt=False)

    proc, _ = _run(
        tmp_path,
        "redirect",
        OPENLAPS_ECU_LOOPS="1",
        OPENLAPS_ECU_POLL_S="0",
        OPENLAPS_ECU_VM="guest",
        OPENLAPS_ECU_VM_PORT="4321",
    )

    assert proc.returncode == 0, proc.stderr
    docker = (tmp_path / "docker.calls").read_text()
    assert "exec guest iptables -t nat -I QEMU_DNAT -p tcp --dport 4321" in docker, (
        "dockur DNATs unknown inbound ports to the guest; ours must be exempted"
    )
    monitor = (tmp_path / "monitor.in").read_text()
    assert (
        "chardev-add socket,id=openlaps_ecu_cd,host=0.0.0.0,port=4321,server=on,wait=off" in monitor
    )
    assert "device_add usb-redir,id=openlaps_ecu,chardev=openlaps_ecu_cd,bus=xhci.0" in monitor
    usbredirect = (tmp_path / "usbredirect.calls").read_text().strip()
    assert usbredirect == "--device 5-7 --to 172.19.0.2:4321 -k"
    assert "redirecting 5-7" in proc.stderr


def test_redirect_waits_while_nothing_is_attached_or_the_vm_is_down(tmp_path):
    (tmp_path / "sys" / "bus").mkdir(parents=True)
    _stub_vm(tmp_path)
    proc, _ = _run(tmp_path, "redirect", OPENLAPS_ECU_LOOPS="2", OPENLAPS_ECU_POLL_S="0")
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "usbredirect.calls").exists()
    assert not (tmp_path / "monitor.in").exists(), "no guest-side binding without a device"
    assert proc.stderr.count("not on this host") == 1, "logged once per absence, not per poll"

    _stub_vm(tmp_path, running=False)
    proc, _ = _run(tmp_path, "redirect", OPENLAPS_ECU_LOOPS="2", OPENLAPS_ECU_POLL_S="0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("is not running") == 1
    assert not (tmp_path / "usbredirect.calls").exists()


def test_the_env_template_names_the_guest_and_port_the_script_defaults_to():
    template = (ROOT / "deploy" / "ecu" / "openlaps-ecu.env").read_text()
    script = SCRIPT.read_text()
    vm = re.search(r"OPENLAPS_ECU_VM:-([^}]+)\}", script).group(1)
    port = re.search(r"OPENLAPS_ECU_VM_PORT:-([^}]+)\}", script).group(1)
    assert f"OPENLAPS_ECU_VM={vm}" in template
    assert f"OPENLAPS_ECU_VM_PORT={port}" in template


def test_usbipd_is_closed_to_everything_but_the_pit():
    rules = (ROOT / "deploy" / "nft" / "ecu-usbip.nft").read_text()
    pit = re.search(r"define PIT_ADDR = (\S+)", rules).group(1)
    assert pit in (ROOT / "docs" / "operations" / "registry.md").read_text()
    accept = rules.index("tcp dport 3240 ip saddr $PIT_ADDR accept")
    drop = rules.index("tcp dport 3240 counter drop")
    assert accept < drop
