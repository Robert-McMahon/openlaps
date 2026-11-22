//! A congestion controller that keeps the radio's queue short.
//!
//! Sending live frames first is not enough. A loss-based controller (CUBIC,
//! Reno) keeps raising its window until the bottleneck's queue overflows, so
//! on a slow radio there are always seconds of stream data already queued
//! *at the radio*, and a live frame sent "first" still waits behind all of
//! it. Measured on the bench: a small datagram flow sharing a 100 kbit/s
//! link with a saturating TCP flow arrived 1.5 to 8 s late.
//!
//! This controller watches delay instead (the LEDBAT idea, RFC 6817). The
//! lowest round-trip time seen recently is taken as the empty-queue delay;
//! anything above it is queue. The window grows while that queue is under
//! the target and shrinks in proportion when it is over, so the stream
//! settles at whatever rate holds the queue at about the target. A live
//! frame then waits at most about that long, whatever the link's capacity.
//!
//! It gives way to loss-based traffic sharing the path. That is the right
//! way round here: the stream is a backlog being caught up, and it is not
//! in a hurry.

use std::any::Any;
use std::sync::Arc;
use std::time::{Duration, Instant};

use quinn::congestion::{Controller, ControllerFactory};
use quinn_proto::RttEstimator;

const MIN_WINDOW_PACKETS: u64 = 2;
/// Small, because on a slow radio even this much is a visible queue.
const INITIAL_WINDOW_PACKETS: u64 = 4;
const MAX_WINDOW: u64 = 8 * 1024 * 1024;
/// The empty-queue delay is re-learned over this long, so that a change of
/// path (radio to satellite, say) is not mistaken for a queue for ever.
const BASE_HISTORY: Duration = Duration::from_secs(300);
/// A stream that saturates the link for longer than the history never sees
/// the queue empty, and would come to take its own standing queue for the
/// empty-queue delay. So now and then it stops for a moment and looks
/// (the LEDBAT++ "periodic slowdown").
const DRAIN_EVERY: Duration = Duration::from_secs(20);
const DRAIN_AT_LEAST: Duration = Duration::from_millis(300);

pub struct LowDelayConfig {
    target: Duration,
}

impl LowDelayConfig {
    pub fn new(target: Duration) -> Self {
        Self { target }
    }
}

impl ControllerFactory for LowDelayConfig {
    fn build(self: Arc<Self>, now: Instant, current_mtu: u16) -> Box<dyn Controller> {
        Box::new(LowDelay {
            target: self.target,
            mtu: u64::from(current_mtu),
            window: INITIAL_WINDOW_PACKETS * u64::from(current_mtu),
            slow_start: true,
            base: [Duration::MAX; 2],
            base_rolled: now,
            cut_at: now,
            credit: 0.0,
            next_drain: now + DRAIN_EVERY,
            drain_until: None,
            resume_window: 0,
        })
    }
}

#[derive(Clone)]
struct LowDelay {
    target: Duration,
    mtu: u64,
    window: u64,
    slow_start: bool,
    /// Lowest delay in the current and the previous history period.
    base: [Duration; 2],
    base_rolled: Instant,
    /// Packets sent before this were already answered for by a window cut.
    cut_at: Instant,
    /// Fractional bytes of window change not yet applied.
    credit: f64,
    next_drain: Instant,
    drain_until: Option<Instant>,
    resume_window: u64,
}

impl LowDelay {
    fn min_window(&self) -> u64 {
        MIN_WINDOW_PACKETS * self.mtu
    }

    fn note_delay(&mut self, now: Instant, delay: Duration) -> Duration {
        if now.saturating_duration_since(self.base_rolled) >= BASE_HISTORY {
            self.base_rolled = now;
            self.base[1] = self.base[0];
            self.base[0] = Duration::MAX;
        }
        self.base[0] = self.base[0].min(delay);
        delay.saturating_sub(self.base[0].min(self.base[1]))
    }
}

impl Controller for LowDelay {
    fn on_ack(
        &mut self,
        now: Instant,
        sent: Instant,
        bytes: u64,
        app_limited: bool,
        rtt: &RttEstimator,
    ) {
        let queue = self.note_delay(now, now.saturating_duration_since(sent));
        if let Some(until) = self.drain_until {
            if now < until {
                return;
            }
            self.drain_until = None;
            self.window = self.resume_window;
            self.next_drain = now + DRAIN_EVERY;
            return;
        }
        if app_limited {
            // An idle sender sees the empty queue all the time.
            self.next_drain = now + DRAIN_EVERY;
        } else if now >= self.next_drain && !self.slow_start {
            self.resume_window = self.window;
            self.window = self.min_window();
            self.drain_until = Some(now + (rtt.get() * 2).max(DRAIN_AT_LEAST));
            return;
        }
        let target = self.target.as_secs_f64();
        let off = ((target - queue.as_secs_f64()) / target).clamp(-1.0, 1.0);
        if queue > self.target * 2 && sent > self.cut_at {
            // Well over: the link got slower under us. Stepping down a packet
            // per round trip would leave live frames late for many seconds.
            self.slow_start = false;
            self.window = (self.window * 3 / 4).max(self.min_window());
            self.cut_at = now;
            return;
        }
        if self.slow_start {
            if off < 0.75 {
                // A quarter of the target is queue already: the link's rate
                // is found, and doubling again would overshoot it.
                self.slow_start = false;
            } else if !app_limited {
                self.window = (self.window + bytes).min(MAX_WINDOW);
                return;
            }
        }
        if app_limited && off > 0.0 {
            // Nothing learned about the link from a sender that is not trying.
            return;
        }
        // LEDBAT: up to one packet per round trip while under target, and a
        // matching retreat when over it.
        self.credit += off * bytes as f64 * self.mtu as f64 / self.window as f64;
        let whole = self.credit.trunc();
        self.credit -= whole;
        let next = self.window as f64 + whole;
        self.window = (next.max(self.min_window() as f64) as u64).min(MAX_WINDOW);
    }

    fn on_congestion_event(
        &mut self,
        now: Instant,
        sent: Instant,
        is_persistent_congestion: bool,
        _lost_bytes: u64,
    ) {
        self.slow_start = false;
        if is_persistent_congestion {
            self.window = self.min_window();
            self.cut_at = now;
        } else if sent > self.cut_at {
            // One cut per round of losses, not one per lost packet.
            self.window = (self.window / 2).max(self.min_window());
            self.cut_at = now;
        }
    }

    fn on_mtu_update(&mut self, new_mtu: u16) {
        self.mtu = u64::from(new_mtu);
        self.window = self.window.max(self.min_window());
    }

    fn window(&self) -> u64 {
        self.window
    }

    fn clone_box(&self) -> Box<dyn Controller> {
        Box::new(self.clone())
    }

    fn initial_window(&self) -> u64 {
        INITIAL_WINDOW_PACKETS * self.mtu
    }

    fn into_any(self: Box<Self>) -> Box<dyn Any> {
        self
    }
}
