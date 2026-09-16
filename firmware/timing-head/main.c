#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "association.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include "nmea.h"
#include "pico/stdlib.h"
#if defined(TIMING_TRANSPORT_USB) || defined(GNSS_DATA_RELAY)
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
#ifndef TIMING_GNSS_TX_GPIO
#define TIMING_GNSS_TX_GPIO 4
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

#if defined(GNSS_DATA_RELAY) && defined(TIMING_TRANSPORT_USB)
#error "GNSS_DATA_RELAY needs TIMING_TRANSPORT=UART: TH1 and NMEA cannot share one CDC"
#endif

/*
 * Receiver bytes are drained by interrupt, never by the main loop alone.
 *
 * Emitting a TH1 line is a blocking 115200 write of ~60 bytes -- about 5 ms.
 * At 50 Hz RMC the receiver produces a byte every ~250 us, so that write
 * spans ~20 bytes and the UART's 32-byte FIFO has almost no margin left. A
 * dropped byte there corrupts a sentence the car actually navigates on, so
 * the FIFO is emptied into a ring under interrupt and the main loop reads
 * from the ring instead.
 *
 * The TH1 write itself stays blocking and immediate on purpose: its
 * edge-to-transmit figure is measured immediately before transmission, so
 * queueing it would make the number the host subtracts a lie.
 */
#define RX_RING_CAPACITY 2048
#define TO_HOST_CAPACITY 4096
#define TO_RECEIVER_CAPACITY 2048
/* Bounded work per pass so no single direction can monopolise the loop. */
#define PUMP_BUDGET 128

typedef struct {
    uint8_t *buffer;
    size_t capacity;
    volatile size_t head;
    volatile size_t tail;
    volatile uint32_t drops;
} ring_t;

static uint8_t rx_storage[RX_RING_CAPACITY];
static ring_t rx_ring = {rx_storage, sizeof(rx_storage), 0, 0, 0};

#ifdef GNSS_DATA_RELAY
static uint8_t to_host_storage[TO_HOST_CAPACITY];
static uint8_t to_receiver_storage[TO_RECEIVER_CAPACITY];
static ring_t to_host = {to_host_storage, sizeof(to_host_storage), 0, 0, 0};
static ring_t to_receiver = {to_receiver_storage, sizeof(to_receiver_storage), 0, 0, 0};
#endif

static bool ring_push(ring_t *ring, uint8_t value) {
    size_t next = (ring->head + 1) % ring->capacity;
    if (next == ring->tail) {
        ring->drops++;
        return false;
    }
    ring->buffer[ring->head] = value;
    ring->head = next;
    return true;
}

static bool ring_pop(ring_t *ring, uint8_t *value) {
    if (ring->tail == ring->head) {
        return false;
    }
    *value = ring->buffer[ring->tail];
    ring->tail = (ring->tail + 1) % ring->capacity;
    return true;
}

static volatile bool edge_pending = false;
static volatile uint64_t edge_time_us = 0;

static void pps_callback(uint gpio, uint32_t events) {
    if (gpio == TIMING_PPS_GPIO && (events & GPIO_IRQ_EDGE_RISE) != 0) {
        edge_time_us = time_us_64();
        edge_pending = true;
    }
}

static void gnss_rx_isr(void) {
    while (uart_is_readable(TIMING_GNSS_UART)) {
        ring_push(&rx_ring, (uint8_t)uart_getc(TIMING_GNSS_UART));
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

static bool rx_ring_pop(uint8_t *value) {
    uint32_t interrupt_state = save_and_disable_interrupts();
    bool present = ring_pop(&rx_ring, value);
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

#ifdef GNSS_DATA_RELAY
/*
 * The data channel: receiver NMEA out, host corrections in, over the RP2040's
 * internal USB CDC. Byte-level passthrough rather than reassembled lines --
 * the agent's transport does its own line framing, and passing bytes through
 * untouched means sentences this firmware does not parse still reach it, as
 * do the UM980 command acknowledgements the driver reads back.
 */
static void relay_init(void) {
    stdio_usb_init();
    uart_set_fifo_enabled(TIMING_GNSS_UART, true);
    gpio_set_function(TIMING_GNSS_TX_GPIO, GPIO_FUNC_UART);
}

static void relay_pump(void) {
    /* Receiver -> host, batched into one CDC call: a per-byte write would be
       128 USB calls per pass at 50 Hz for no benefit. Dropping the backlog
       when nothing is listening is deliberate -- stale position is worthless,
       and blocking here would delay TH1. */
    if (stdio_usb_connected()) {
        char batch[PUMP_BUDGET];
        int count = 0;
        uint8_t value;
        while (count < PUMP_BUDGET && ring_pop(&to_host, &value)) {
            batch[count++] = (char)value;
        }
        if (count > 0) {
            stdio_put_string(batch, count, false, false);
        }
    } else {
        to_host.tail = to_host.head;
    }

    /* Host -> receiver: RTCM corrections and UM980 startup commands. */
    for (int budget = 0; budget < PUMP_BUDGET; budget++) {
        int character = getchar_timeout_us(0);
        if (character == PICO_ERROR_TIMEOUT) {
            break;
        }
        ring_push(&to_receiver, (uint8_t)character);
    }
    while (uart_is_writable(TIMING_GNSS_UART)) {
        uint8_t value;
        if (!ring_pop(&to_receiver, &value)) {
            break;
        }
        uart_putc_raw(TIMING_GNSS_UART, (char)value);
    }
}
#else
static void relay_init(void) {}
static void relay_pump(void) {}
#endif

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
    relay_init();

    /* Derived, not hardcoded to UART1_IRQ: TIMING_GNSS_UART is overridable,
       and a mismatch here would silently never fire the handler. */
    const uint gnss_irq = uart_get_index(TIMING_GNSS_UART) == 0 ? UART0_IRQ : UART1_IRQ;
    irq_set_exclusive_handler(gnss_irq, gnss_rx_isr);
    irq_set_enabled(gnss_irq, true);
    uart_set_irq_enables(TIMING_GNSS_UART, true, false);

    /*
     * An explicit priority order, because the relay put a third interrupt
     * source on this core and RP2040 defaults every one of them to 0x80 --
     * equal priority means no preemption, so whichever handler is already
     * running delays the others until it finishes.
     *
     *   PPS   (IO_IRQ_BANK0) highest: it timestamps the edge, and every
     *                        microsecond it is held off is error the host
     *                        cannot subtract, against an 82 us std dev.
     *   UART1 RX             middle:  losing receiver bytes corrupts a
     *                        sentence the car navigates on.
     *   USB   (default 0x80) lowest:  the relay is bulk data with buffers
     *                        behind it and nothing time-critical in it.
     */
    irq_set_priority(IO_IRQ_BANK0, 0x00);
    irq_set_priority(gnss_irq, 0x40);

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
        relay_pump();

        uint8_t byte;
        if (!rx_ring_pop(&byte)) {
            tight_loop_contents();
            continue;
        }
#ifdef GNSS_DATA_RELAY
        ring_push(&to_host, byte);
#endif
        char character = (char)byte;
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
