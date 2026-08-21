"""Static checks for the P4.8 chrony and timing-head deployment contract."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_vehicle_chrony_prefers_sock_refclock_and_keeps_ntp_fallback():
    config = (ROOT / "deploy" / "chrony" / "vehicle.conf").read_text(encoding="utf-8")
    assert "refclock SOCK /run/chrony/openlaps-timing.sock" in config
    assert "prefer" in config
    assert "pool pool.ntp.org iburst" in config
    assert "local stratum 10" in config
    assert "allow 192.168.12.0/24" in config


def test_pit_chrony_uses_the_vehicle_sbc():
    config = (ROOT / "deploy" / "chrony" / "pit.conf").read_text(encoding="utf-8")
    assert "server 192.168.12.176 iburst prefer" in config


def test_pit_install_docs_install_and_restart_chrony():
    install_command = "sudo install -m 0644 deploy/chrony/pit.conf /etc/chrony/chrony.conf"
    for path in (ROOT / "deploy" / "README.md", ROOT / "docs" / "BENCH_RUNBOOK.md"):
        instructions = path.read_text(encoding="utf-8")
        assert install_command in instructions
        assert "sudo systemctl restart chrony" in instructions


def test_timing_head_service_waits_for_chrony_and_restarts():
    unit = (ROOT / "deploy" / "systemd" / "timing-head-shim.service").read_text(encoding="utf-8")
    assert "After=chrony.service" in unit
    assert "Requires=chrony.service" in unit
    assert "PartOf=chrony.service" in unit
    assert "Restart=always" in unit
    assert "/usr/bin/python3 /usr/local/libexec/openlaps/timing_head_shim.py" in unit

    drop_in = (ROOT / "deploy" / "systemd" / "chrony-openlaps-sock.conf").read_text(
        encoding="utf-8"
    )
    assert "Group=openlaps" in drop_in
    assert "ExecStart=!/usr/bin/setpriv --regid=openlaps --clear-groups" in drop_in
    assert "UMask=0117" in drop_in
    assert "RuntimeDirectoryMode=0750" in drop_in
    assert "ExecStartPre" not in unit


def test_uart_build_uses_the_x4_internal_uart_and_leaves_uart1_for_the_um980():
    source = (ROOT / "firmware" / "timing-head" / "main.c").read_text(encoding="utf-8")
    readme = (ROOT / "firmware" / "timing-head" / "README.md").read_text(encoding="utf-8")

    assert "#define TIMING_HOST_UART uart0" in source
    assert "#define TIMING_HOST_TX_GPIO 0" in source
    assert "#define TIMING_GNSS_UART uart1" in source
    assert "#define TIMING_GNSS_RX_GPIO 5" in source
    assert "/dev/ttyS4" in readme
    assert "no external host-UART wire" in readme
