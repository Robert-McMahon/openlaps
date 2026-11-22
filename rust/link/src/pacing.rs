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

impl LowDelay {
    /// One acknowledgement: `bytes` sent at `sent` were confirmed at `now`.
    fn acked(
        &mut self,
        now: Instant,
        sent: Instant,
        bytes: u64,
        app_limited: bool,
        smoothed_rtt: Duration,
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
            self.drain_until = Some(now + (smoothed_rtt * 2).max(DRAIN_AT_LEAST));
            return;
        }
        let target = self.target.as_secs_f64();
        let off = ((target - queue.as_secs_f64()) / target).clamp(-1.0, 1.0);
        if queue > self.target * 2 && sent > self.cut_at {
            // Well over: the link got slower under us. Stepping down a packet
            // per round trip would leave live frames late for many seconds,
            // and so would a fixed cut when the link has lost most of its
            // capacity at once (a round trip is then seconds long, and each
            // cut waits for one). The queue is about window/rate deep, so
            // the window that would hold it at twice the target is the
            // present one scaled by that ratio.
            self.slow_start = false;
            let scale = (target * 2.0 / queue.as_secs_f64()).clamp(0.2, 0.75);
            self.window = ((self.window as f64 * scale) as u64).max(self.min_window());
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
        self.acked(now, sent, bytes, app_limited, rtt.get());
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

#[cfg(test)]
mod tests {
    use super::*;

    const MTU: u64 = 1200;

    fn controller(now: Instant) -> LowDelay {
        let built =
            Arc::new(LowDelayConfig::new(Duration::from_millis(150))).build(now, MTU as u16);
        *built.into_any().downcast::<LowDelay>().unwrap()
    }

    /// Feed acknowledgements whose delay is `base + queue`, one per 10 ms.
    fn run(c: &mut LowDelay, now: &mut Instant, acks: u32, queue_ms: u64, app_limited: bool) {
        let delay = Duration::from_millis(20 + queue_ms);
        for _ in 0..acks {
            *now += Duration::from_millis(10);
            c.acked(*now, *now - delay, MTU, app_limited, delay);
        }
    }

    #[test]
    fn an_empty_queue_lets_the_window_grow() {
        let mut now = Instant::now();
        let mut c = controller(now);
        let start = c.window();
        run(&mut c, &mut now, 100, 0, false);
        assert!(
            c.window() > start * 4,
            "slow start should have doubled it several times"
        );
    }

    #[test]
    fn a_queue_over_the_target_shrinks_the_window_to_the_floor() {
        let mut now = Instant::now();
        let mut c = controller(now);
        run(&mut c, &mut now, 200, 0, false);
        let grown = c.window();
        run(&mut c, &mut now, 1500, 400, false);
        assert!(c.window() < grown / 4);
        run(&mut c, &mut now, 20_000, 400, false);
        assert_eq!(c.window(), MIN_WINDOW_PACKETS * MTU);
    }

    #[test]
    fn a_queue_at_the_target_holds_the_window() {
        let mut now = Instant::now();
        let mut c = controller(now);
        run(&mut c, &mut now, 50, 0, false);
        run(&mut c, &mut now, 50, 60, false); // leaves slow start
        let held = c.window();
        run(&mut c, &mut now, 1500, 150, false);
        let drift = c.window().abs_diff(held);
        assert!(
            drift <= 2 * MTU,
            "window moved {drift} B at the target delay"
        );
    }

    #[test]
    fn an_idle_sender_does_not_grow_the_window() {
        let mut now = Instant::now();
        let mut c = controller(now);
        let start = c.window();
        run(&mut c, &mut now, 500, 0, true);
        assert_eq!(c.window(), start);
    }

    #[test]
    fn a_busy_sender_pauses_now_and_then_to_see_the_empty_queue() {
        let mut now = Instant::now();
        let mut c = controller(now);
        run(&mut c, &mut now, 50, 0, false);
        run(&mut c, &mut now, 50, 60, false);
        let mut paused = false;
        for _ in 0..3_000 {
            run(&mut c, &mut now, 1, 100, false);
            paused |= c.window() == MIN_WINDOW_PACKETS * MTU;
        }
        assert!(paused, "30 s of saturation without one drain");
        // ... and comes back from the pause.
        run(&mut c, &mut now, 100, 100, false);
        assert!(c.window() > MIN_WINDOW_PACKETS * MTU);
    }

    #[test]
    fn a_collapse_in_capacity_is_answered_in_a_few_round_trips() {
        let mut now = Instant::now();
        let mut c = controller(now);
        run(&mut c, &mut now, 400, 0, false);
        let grown = c.window();
        // Three seconds of queue: twenty times the target. Round trips are
        // now three seconds long, so only a handful of cuts can happen.
        let delay = Duration::from_millis(3020);
        for _ in 0..4 {
            now += delay + Duration::from_millis(10);
            c.acked(now, now - delay, MTU, false, delay);
        }
        assert!(c.window() < grown / 100, "{} of {grown} left", c.window());
    }

    #[test]
    fn loss_halves_the_window_once_per_round() {
        let mut now = Instant::now();
        let mut c = controller(now);
        run(&mut c, &mut now, 200, 0, false);
        let grown = c.window();
        let sent = now;
        now += Duration::from_millis(30);
        c.on_congestion_event(now, sent, false, MTU);
        c.on_congestion_event(now, sent, false, MTU);
        assert_eq!(c.window(), grown / 2);
    }
}
