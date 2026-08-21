#include "association.h"

bool timing_associate(bool edge_present, uint64_t edge_us, uint64_t sentence_us,
                      uint64_t *delay_us) {
    if (!edge_present || delay_us == 0 || sentence_us < edge_us) {
        return false;
    }
    uint64_t delay = sentence_us - edge_us;
    if (delay > TIMING_MAX_SENTENCE_DELAY_US) {
        return false;
    }
    *delay_us = delay;
    return true;
}
