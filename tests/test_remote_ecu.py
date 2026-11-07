"""Remote ECU tuning over USB/IP (docs/operations/remote-ecu.md).

The script runs as root on both hosts against live sysfs and the usbip CLI,
so these tests drive it against a fake sysfs tree and a stub `usbip` on PATH.
The rest pins the properties the runbook's safety section relies on: the
vehicle export cannot be enabled at boot, usbipd dies with it, the Windows
guest never starts with a plain `up -d`, and every piece reads the same IDs.
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
    for name in ("openlaps-ecu-export.service", "openlaps-ecu-attach.service"):
        assert f"EnvironmentFile={default}" in (SYSTEMD / name).read_text(), name


def test_the_windows_guest_is_opt_in_and_survives_ecu_resets():
    compose = yaml.safe_load((ROOT / "deploy" / "pit-compose.yaml").read_text())
    svc = compose["services"]["ecu-windows"]
    assert svc["profiles"] == ["ecu"], "a plain `up -d` must not boot a Windows VM"
    # A bind mount plus a cgroup rule for the USB major, not a `devices:`
    # entry, so the node a reset ECU comes back on is visible and allowed.
    assert "/dev/bus/usb:/dev/bus/usb" in svc["volumes"]
    assert "c 189:* rmw" in svc["device_cgroup_rules"]
    assert not any("/dev/bus/usb" in d for d in svc["devices"])
    args = svc["environment"]["ARGUMENTS"]
    assert "vendorid=0x${OPENLAPS_ECU_USB_VID" in args
    assert "productid=0x${OPENLAPS_ECU_USB_PID" in args


def test_usbipd_is_closed_to_everything_but_the_pit():
    rules = (ROOT / "deploy" / "nft" / "ecu-usbip.nft").read_text()
    pit = re.search(r"define PIT_ADDR = (\S+)", rules).group(1)
    assert pit in (ROOT / "docs" / "operations" / "registry.md").read_text()
    accept = rules.index("tcp dport 3240 ip saddr $PIT_ADDR accept")
    drop = rules.index("tcp dport 3240 counter drop")
    assert accept < drop
