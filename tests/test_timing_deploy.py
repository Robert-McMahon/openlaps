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


def test_the_timing_shim_install_carries_the_module_it_imports():
    """`timing_head_shim.py` alone is no longer a complete install.

    The chrony SOCK wire format moved to `chrony_sock.py` when a second time
    source (`pps_gpio_shim.py`) started feeding the same socket -- one copy of
    the offset's sign, not two. Python finds it because both land in the same
    directory, which only happens if the runbook says to put them there.
    """
    for doc in (ROOT / "deploy" / "README.md", ROOT / "docs" / "BENCH_RUNBOOK.md"):
        text = doc.read_text()
        assert "/usr/local/libexec/openlaps/timing_head_shim.py" in text
        assert "/usr/local/libexec/openlaps/chrony_sock.py" in text, f"{doc.name}"


def test_the_two_time_sources_refuse_to_run_together():
    """One chrony socket, one opinion about the second.

    Both shims send `struct sock_sample` to the same refclock. Two of them at
    once is not redundancy -- chrony has no way to tell the samples apart, so
    it averages two different measurements of the same edge.
    """
    unit = (ROOT / "deploy" / "systemd" / "openlaps-pps-gpio.service").read_text()

    assert "Conflicts=timing-head-shim.service" in unit
    assert "/usr/local/libexec/openlaps/pps_gpio_shim.py" in unit
    assert "EnvironmentFile=/etc/openlaps/pps-gpio.env" in unit


def test_the_gpio_udev_rule_grants_the_group_the_shim_runs_as():
    """The shim runs as `openlaps`; /dev/gpiochip* is root-only as shipped."""
    rule = (ROOT / "deploy" / "udev" / "99-openlaps-gpio.rules").read_text()
    unit = (ROOT / "deploy" / "systemd" / "openlaps-pps-gpio.service").read_text()

    assert 'SUBSYSTEM=="gpio"' in rule
    assert 'GROUP="openlaps"' in rule
    assert "User=openlaps" in unit


def test_the_two_vehicle_chrony_configs_differ_only_in_the_refclock():
    """One board captures PPS in the kernel, the other takes samples on a socket.

    Everything else about a vehicle's clock -- the fallback pool, the step
    policy, the stratum it falls back to, who it serves -- is the same
    decision on both, so a change to one that is not made to the other is a
    drift nobody would notice until an event.
    """
    sock = (ROOT / "deploy" / "chrony" / "vehicle.conf").read_text()
    pps = (ROOT / "deploy" / "chrony" / "vehicle-pps.conf").read_text()

    def settings(text: str) -> list[str]:
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#") and not line.startswith("refclock")
        ]

    assert settings(sock) == settings(pps)
    assert "refclock SOCK /run/chrony/openlaps-timing.sock" in sock
    assert "refclock PPS /dev/pps0" in pps


def test_the_kernel_pps_config_does_not_lock_to_a_refclock_that_is_absent():
    """`lock NMEA` needs a second refclock; this board has none feeding chrony."""
    pps = (ROOT / "deploy" / "chrony" / "vehicle-pps.conf").read_text()
    directives = [line for line in pps.splitlines() if line.strip() and not line.startswith("#")]

    # `lock` as an option word, not as the tail of `refclock`.
    assert not any("lock" in line.split() for line in directives)
    assert any(line.startswith("pool pool.ntp.org") for line in directives)
