#ifndef OPENLAPS_TIMING_NMEA_H
#define OPENLAPS_TIMING_NMEA_H

#include <stdbool.h>
#include <stdint.h>

bool nmea_gga_has_fix(const char *sentence);
bool nmea_zda_utc_second(const char *sentence, int64_t *utc_second);

#endif
