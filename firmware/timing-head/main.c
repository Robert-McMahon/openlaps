#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "association.h"
#include "hardware/gpio.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include "nmea.h"
#include "pico/stdlib.h"
#ifdef TIMING_TRANSPORT_USB
#include "pico/stdio_usb.h"
#endif

#ifndef TIMING_PPS_GPIO
#define TIMING_PPS_GPIO 2
#endif
#ifndef TIMING_GNSS_UART
#define TIMING_GNSS_UART uart1
#endif
#ifndef TIMING_GNSS_RX_GPIO
#define TIMING_GNSS_RX_GPIO 5
#endif
#ifndef TIMING_HOST_UART
#define TIMING_HOST_UART uart0
#endif
#ifndef TIMING_HOST_TX_GPIO
#define TIMING_HOST_TX_GPIO 0
#endif
#ifndef TIMING_BAUD
#define TIMING_BAUD 115200
#endif

#define LINE_CAPACITY 128
#define FIX_FRESH_US UINT64_C(1500000)

static volatile bool edge_pending = false;
static volatile uint64_t edge_time_us = 0;

static void pps_callback(uint gpio, uint32_t events) {
    if (gpio == TIMING_PPS_GPIO && (events & GPIO_IRQ_EDGE_RISE) != 0) {
        edge_time_us = time_us_64();
        edge_pending = true;
    }
}

static bool take_edge(uint64_t *edge_us) {
    uint32_t interrupt_state = save_and_disable_interrupts();
    bool present = edge_pending;
    if (present) {
        *edge_us = edge_time_us;
        edge_pending = false;
    }
    restore_interrupts(interrupt_state);
    return present;
}

static void host_init(void) {
#ifdef TIMING_TRANSPORT_USB
    stdio_usb_init();
#else
    uart_init(TIMING_HOST_UART, TIMING_BAUD);
    gpio_set_function(TIMING_HOST_TX_GPIO, GPIO_FUNC_UART);
#endif
}

static void host_write(const char *message, size_t length) {
#ifdef TIMING_TRANSPORT_USB
    stdio_put_string(message, (int)length, false, true);
#else
    uart_write_blocking(TIMING_HOST_UART, (const uint8_t *)message, length);
#endif
}

static void write_u64_fixed(char *field, size_t width, uint64_t value) {
    while (width > 0) {
        field[--width] = (char)('0' + value % 10);
        value /= 10;
    }
}

static bool is_sentence(const char *line, const char *kind) {
    return strlen(line) >= 6 && line[0] == '$' && strncmp(line + 3, kind, 3) == 0;
}

int main(void) {
    host_init();
    uart_init(TIMING_GNSS_UART, TIMING_BAUD);
    gpio_set_function(TIMING_GNSS_RX_GPIO, GPIO_FUNC_UART);

    gpio_init(TIMING_PPS_GPIO);
    gpio_set_dir(TIMING_PPS_GPIO, GPIO_IN);
    gpio_pull_down(TIMING_PPS_GPIO);
    gpio_set_irq_enabled_with_callback(TIMING_PPS_GPIO, GPIO_IRQ_EDGE_RISE, true, pps_callback);

    char line[LINE_CAPACITY];
    size_t used = 0;
    bool fixed = false;
    uint64_t fix_seen_us = 0;
    uint32_t sequence = 0;

    while (true) {
        if (!uart_is_readable(TIMING_GNSS_UART)) {
            tight_loop_contents();
            continue;
        }
        char character = (char)uart_getc(TIMING_GNSS_UART);
        if (character != '\n' && character != '\r') {
            if (used + 1 < sizeof(line)) {
                line[used++] = character;
            } else {
                used = 0;
            }
            continue;
        }
        if (used == 0) {
            continue;
        }
        line[used] = '\0';
        used = 0;
        uint64_t sentence_us = time_us_64();

        if (is_sentence(line, "GGA")) {
            fixed = nmea_gga_has_fix(line);
            fix_seen_us = sentence_us;
            continue;
        }
        if (!is_sentence(line, "ZDA")) {
            continue;
        }

        int64_t utc_second = 0;
        uint64_t edge_us = 0;
        uint64_t sentence_delay_us = 0;
        bool has_edge = take_edge(&edge_us);
        bool associated =
            timing_associate(has_edge, edge_us, sentence_us, &sentence_delay_us);
        bool fix_is_fresh = fixed && sentence_us >= fix_seen_us &&
                            sentence_us - fix_seen_us <= FIX_FRESH_US;
        if (!associated || !fix_is_fresh || !nmea_zda_utc_second(line, &utc_second)) {
            continue;
        }

        char message[128];
        int prefix_length = snprintf(message, sizeof(message), "TH1 %lu %lld %llu ",
                                     (unsigned long)sequence++, (long long)utc_second,
                                     (unsigned long long)edge_us);
        if (prefix_length <= 0 || (size_t)prefix_length + 24 >= sizeof(message)) {
            continue;
        }
        size_t delay_offset = (size_t)prefix_length;
        memset(message + delay_offset, '0', 20);
        int suffix_length = snprintf(message + delay_offset + 20,
                                     sizeof(message) - delay_offset - 20, " 1\n");
        if (suffix_length > 0) {
            size_t length = delay_offset + 20 + (size_t)suffix_length;
            uint64_t transmit_us = time_us_64();
            write_u64_fixed(message + delay_offset, 20, transmit_us - edge_us);
            host_write(message, length);
        }
    }
}
