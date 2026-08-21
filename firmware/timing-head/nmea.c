#include "nmea.h"

#include <stddef.h>
#include <stdlib.h>
#include <string.h>

static bool checksum_valid(const char *sentence) {
    if (sentence == NULL || sentence[0] != '$') {
        return false;
    }
    const char *star = strchr(sentence, '*');
    if (star == NULL || star[1] == '\0' || star[2] == '\0') {
        return false;
    }
    unsigned checksum = 0;
    for (const char *cursor = sentence + 1; cursor < star; ++cursor) {
        checksum ^= (unsigned char)*cursor;
    }
    char supplied[3] = {star[1], star[2], '\0'};
    char *end = NULL;
    unsigned long parsed = strtoul(supplied, &end, 16);
    return end == supplied + 2 && parsed == checksum;
}

static const char *field(const char *sentence, unsigned index) {
    const char *cursor = sentence;
    for (unsigned current = 0; current < index; ++current) {
        cursor = strchr(cursor, ',');
        if (cursor == NULL) {
            return NULL;
        }
        ++cursor;
    }
    return cursor;
}

bool nmea_gga_has_fix(const char *sentence) {
    if (!checksum_valid(sentence) || strlen(sentence) < 6 ||
        strncmp(sentence + 3, "GGA", 3) != 0) {
        return false;
    }
    const char *quality = field(sentence, 6);
    return quality != NULL && quality[0] >= '1' && quality[0] <= '9' &&
           (quality[1] == ',' || quality[1] == '*');
}

static bool leap_year(int year) {
    return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
}

static int64_t days_before_year(int year) {
    int64_t y = year - 1;
    return 365 * y + y / 4 - y / 100 + y / 400;
}

static int64_t unix_second(int year, int month, int day, int hour, int minute, int second) {
    static const int cumulative_days[] = {0,  0,  31, 59, 90, 120, 151,
                                          181, 212, 243, 273, 304, 334};
    int64_t days = days_before_year(year) - days_before_year(1970);
    days += cumulative_days[month] + day - 1;
    if (month > 2 && leap_year(year)) {
        ++days;
    }
    return days * 86400 + hour * 3600 + minute * 60 + second;
}

bool nmea_zda_utc_second(const char *sentence, int64_t *utc_second_out) {
    if (utc_second_out == NULL || !checksum_valid(sentence) || strlen(sentence) < 6 ||
        strncmp(sentence + 3, "ZDA", 3) != 0) {
        return false;
    }
    const char *time_value = field(sentence, 1);
    const char *day_value = field(sentence, 2);
    const char *month_value = field(sentence, 3);
    const char *year_value = field(sentence, 4);
    if (time_value == NULL || day_value == NULL || month_value == NULL || year_value == NULL) {
        return false;
    }
    char *end = NULL;
    double hhmmss = strtod(time_value, &end);
    if (end == time_value || *end != ',') {
        return false;
    }
    int packed = (int)hhmmss;
    int hour = packed / 10000;
    int minute = (packed / 100) % 100;
    int second = packed % 100;
    int day = atoi(day_value);
    int month = atoi(month_value);
    int year = atoi(year_value);
    if (year < 1970 || month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 ||
        minute > 59 || second > 60) {
        return false;
    }
    *utc_second_out = unix_second(year, month, day, hour, minute, second);
    return true;
}
