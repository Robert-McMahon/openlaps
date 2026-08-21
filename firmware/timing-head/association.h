#ifndef OPENLAPS_TIMING_ASSOCIATION_H
#define OPENLAPS_TIMING_ASSOCIATION_H

#include <stdbool.h>
#include <stdint.h>

#define TIMING_MAX_SENTENCE_DELAY_US UINT64_C(900000)

bool timing_associate(bool edge_present, uint64_t edge_us, uint64_t sentence_us,
                      uint64_t *delay_us);

#endif
