"""Host-native tests for the timing-head's edge/sentence association."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FIRMWARE = ROOT / "firmware" / "timing-head"


def test_edge_sentence_association_rejects_late_missing_and_preceding_sentences(tmp_path: Path):
    harness = tmp_path / "association_test.c"
    harness.write_text(
        r"""
#include <assert.h>
#include <stdint.h>
#include "association.h"

int main(void) {
    uint64_t delay = 0;
    assert(timing_associate(true, 1000000, 1000000, &delay));
    assert(delay == 0);
    assert(timing_associate(true, 1000000, 1900000, &delay));
    assert(delay == 900000);
    assert(!timing_associate(true, 1000000, 1900001, &delay));
    assert(!timing_associate(true, 1000000, 999999, &delay));
    assert(!timing_associate(false, 1000000, 1100000, &delay));
    return 0;
}
""",
        encoding="utf-8",
    )
    executable = tmp_path / "association_test"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{FIRMWARE}",
            str(harness),
            str(FIRMWARE / "association.c"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)


def test_zda_names_utc_second_and_gga_gates_fix_validity(tmp_path: Path):
    harness = tmp_path / "nmea_test.c"
    harness.write_text(
        r"""
#include <assert.h>
#include <stdint.h>
#include "nmea.h"

int main(void) {
    int64_t utc = 0;
    assert(nmea_zda_utc_second("$GNZDA,054931.00,30,12,2021,,*73", &utc));
    assert(utc == INT64_C(1640843371));
    assert(!nmea_zda_utc_second("$GNZDA,054931.00,30,12,2021,,*00", &utc));
    assert(nmea_gga_has_fix("$GNGGA,054931.00,3148.0000,S,11548.0000,E,4,12,0.8,10.0,M,0.0,M,,*6D"));
    assert(!nmea_gga_has_fix("$GNGGA,054931.00,3148.0000,S,11548.0000,E,0,00,99.9,10.0,M,0.0,M,,*5B"));
    return 0;
}
""",
        encoding="utf-8",
    )
    executable = tmp_path / "nmea_test"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{FIRMWARE}",
            str(harness),
            str(FIRMWARE / "nmea.c"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
