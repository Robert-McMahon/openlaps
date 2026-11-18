//! The reader thread: one SocketCAN socket, drained with `recvmmsg`.

use std::mem;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::engine::Engine;

const BATCH: usize = 64;
const CANFD_MTU: usize = 72;
const CONTROL_LEN: usize = 64;
const CAN_EFF_FLAG: u32 = 0x8000_0000;
const CAN_RTR_FLAG: u32 = 0x4000_0000;
const CAN_ERR_FLAG: u32 = 0x2000_0000;
const CAN_EFF_MASK: u32 = 0x1FFF_FFFF;
const CAN_SFF_MASK: u32 = 0x0000_07FF;
const SO_MEMINFO: libc::c_int = 55;
const SK_MEMINFO_VARS: usize = 9;
const SK_MEMINFO_DROPS: usize = 8;
const POLL_MS: libc::c_int = 200;
const DROPS_INTERVAL_NS: i64 = 250_000_000;
const BACKOFF_START: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(30);
/// A realtime-minus-monotonic reading is trusted only if the two clocks were
/// read this close together, and a change this large is a clock step.
const CLOCK_BRACKET_NS: i64 = 100_000;
const CLOCK_STEP_NS: i64 = 250_000;

#[derive(Default)]
pub struct Transport {
    pub open_failures: AtomicU64,
    pub bus_errors: AtomicU64,
    pub reconnects: AtomicU64,
    pub socket_drops: AtomicU64,
    pub recv_buffer_bytes: AtomicU64,
    pub clock_steps: AtomicU64,
    pub capture_lag_ns: AtomicI64,
    pub capture_lag_max_ns: AtomicI64,
}

pub struct Shared {
    pub engine: Mutex<Engine>,
    pub transport: Transport,
    pub stop: AtomicBool,
    pub running: AtomicBool,
}

fn clock_ns(clock: libc::clockid_t) -> i64 {
    let mut ts: libc::timespec = unsafe { mem::zeroed() };
    unsafe { libc::clock_gettime(clock, &mut ts) };
    ts.tv_sec as i64 * 1_000_000_000 + ts.tv_nsec as i64
}

pub fn monotonic_ns() -> i64 {
    clock_ns(libc::CLOCK_MONOTONIC)
}

pub fn realtime_ns() -> i64 {
    clock_ns(libc::CLOCK_REALTIME)
}

/// Kernel frame stamps are realtime; this keeps them on the monotonic
/// timebase across a clock step (see `_KernelStampClock` in can.py).
struct StampClock {
    offset_ns: Option<i64>,
    steps: u64,
}

impl StampClock {
    fn resync(&mut self) {
        let before = monotonic_ns();
        let realtime = realtime_ns();
        let after = monotonic_ns();
        if after - before > CLOCK_BRACKET_NS {
            if self.offset_ns.is_none() {
                self.offset_ns = Some(realtime - after);
            }
            return;
        }
        let offset = realtime - (before + after) / 2;
        match self.offset_ns {
            None => self.offset_ns = Some(offset),
            Some(current) if (offset - current).abs() > CLOCK_STEP_NS => {
                self.steps += 1;
                self.offset_ns = Some(offset);
            }
            Some(_) => {}
        }
    }

    fn to_mono_ns(&mut self, stamp_ns: i64) -> i64 {
        if self.offset_ns.is_none() {
            self.resync();
        }
        stamp_ns - self.offset_ns.unwrap_or(0)
    }
}

struct Fd(libc::c_int);

impl Drop for Fd {
    fn drop(&mut self) {
        unsafe { libc::close(self.0) };
    }
}

fn set_int(fd: libc::c_int, level: libc::c_int, name: libc::c_int, value: libc::c_int) -> bool {
    let rc = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            &value as *const _ as *const libc::c_void,
            mem::size_of::<libc::c_int>() as libc::socklen_t,
        )
    };
    rc == 0
}

fn open(interface: &str, recv_buffer_bytes: usize, transport: &Transport) -> Option<Fd> {
    let name = std::ffi::CString::new(interface).ok()?;
    let index = unsafe { libc::if_nametoindex(name.as_ptr()) };
    if index == 0 {
        return None;
    }
    let raw = unsafe { libc::socket(libc::PF_CAN, libc::SOCK_RAW | libc::SOCK_CLOEXEC, libc::CAN_RAW) };
    if raw < 0 {
        return None;
    }
    let fd = Fd(raw);
    // Ask for the buffer before binding so nothing is queued against the
    // default one. The kernel silently caps this at net.core.rmem_max.
    set_int(fd.0, libc::SOL_SOCKET, libc::SO_RCVBUF, recv_buffer_bytes.min(i32::MAX as usize) as libc::c_int);
    if !set_int(fd.0, libc::SOL_SOCKET, libc::SO_TIMESTAMPNS, 1) {
        return None;
    }
    set_int(fd.0, libc::SOL_CAN_RAW, libc::CAN_RAW_FD_FRAMES, 1);
    let mut address: libc::sockaddr_can = unsafe { mem::zeroed() };
    address.can_family = libc::AF_CAN as libc::sa_family_t;
    address.can_ifindex = index as libc::c_int;
    let rc = unsafe {
        libc::bind(
            fd.0,
            &address as *const _ as *const libc::sockaddr,
            mem::size_of::<libc::sockaddr_can>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        return None;
    }
    let mut granted: libc::c_int = 0;
    let mut length = mem::size_of::<libc::c_int>() as libc::socklen_t;
    unsafe {
        libc::getsockopt(
            fd.0,
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            &mut granted as *mut _ as *mut libc::c_void,
            &mut length,
        )
    };
    transport.recv_buffer_bytes.store(granted.max(0) as u64, Ordering::Relaxed);
    Some(fd)
}

fn socket_drops(fd: libc::c_int) -> u64 {
    let mut info = [0u32; SK_MEMINFO_VARS];
    let mut length = mem::size_of_val(&info) as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            SO_MEMINFO,
            info.as_mut_ptr() as *mut libc::c_void,
            &mut length,
        )
    };
    if rc != 0 || (length as usize) < mem::size_of_val(&info) {
        return 0;
    }
    info[SK_MEMINFO_DROPS] as u64
}

fn stamp_of(header: &libc::msghdr) -> i64 {
    unsafe {
        let mut cmsg = libc::CMSG_FIRSTHDR(header);
        while !cmsg.is_null() {
            if (*cmsg).cmsg_level == libc::SOL_SOCKET && (*cmsg).cmsg_type == libc::SO_TIMESTAMPNS {
                let ts = std::ptr::read_unaligned(libc::CMSG_DATA(cmsg) as *const libc::timespec);
                return ts.tv_sec as i64 * 1_000_000_000 + ts.tv_nsec as i64;
            }
            cmsg = libc::CMSG_NXTHDR(header, cmsg);
        }
    }
    0
}

/// Read until `stop` or a socket error; true if any frame arrived.
fn read_until_error(fd: &Fd, shared: &Shared, drops_before: u64) -> bool {
    let transport = &shared.transport;
    let mut frames = vec![[0u8; CANFD_MTU]; BATCH];
    let mut controls = vec![[0u8; CONTROL_LEN]; BATCH];
    let mut iovecs: Vec<libc::iovec> = frames
        .iter_mut()
        .map(|frame| libc::iovec { iov_base: frame.as_mut_ptr() as *mut libc::c_void, iov_len: CANFD_MTU })
        .collect();
    let mut headers: Vec<libc::mmsghdr> = (0..BATCH).map(|_| unsafe { mem::zeroed() }).collect();
    let mut clock = StampClock { offset_ns: None, steps: transport.clock_steps.load(Ordering::Relaxed) };
    let mut received = false;
    let mut drops_read_ns = 0i64;
    let mut poll_fd = libc::pollfd { fd: fd.0, events: libc::POLLIN, revents: 0 };

    while !shared.stop.load(Ordering::Relaxed) {
        poll_fd.revents = 0;
        let ready = unsafe { libc::poll(&mut poll_fd, 1, POLL_MS) };
        // On a busy bus the loop wakes for almost every frame; the drop
        // counter only needs reading a few times a second.
        let now = monotonic_ns();
        if now - drops_read_ns >= DROPS_INTERVAL_NS {
            drops_read_ns = now;
            transport.socket_drops.store(drops_before + socket_drops(fd.0), Ordering::Relaxed);
        }
        if ready < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            transport.bus_errors.fetch_add(1, Ordering::Relaxed);
            return received;
        }
        if ready == 0 {
            continue;
        }
        if poll_fd.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0
            && poll_fd.revents & libc::POLLIN == 0
        {
            transport.bus_errors.fetch_add(1, Ordering::Relaxed);
            return received;
        }
        loop {
            for index in 0..BATCH {
                let header = &mut headers[index].msg_hdr;
                header.msg_name = std::ptr::null_mut();
                header.msg_namelen = 0;
                header.msg_iov = &mut iovecs[index];
                header.msg_iovlen = 1;
                header.msg_control = controls[index].as_mut_ptr() as *mut libc::c_void;
                header.msg_controllen = CONTROL_LEN as _;
                header.msg_flags = 0;
            }
            let count = unsafe {
                libc::recvmmsg(
                    fd.0,
                    headers.as_mut_ptr(),
                    BATCH as libc::c_uint,
                    libc::MSG_DONTWAIT,
                    std::ptr::null_mut(),
                )
            };
            if count < 0 {
                let error = std::io::Error::last_os_error();
                match error.kind() {
                    std::io::ErrorKind::WouldBlock => break,
                    std::io::ErrorKind::Interrupted => continue,
                    _ => {
                        transport.bus_errors.fetch_add(1, Ordering::Relaxed);
                        return received;
                    }
                }
            }
            if count == 0 {
                break;
            }
            clock.resync();
            transport.clock_steps.store(clock.steps, Ordering::Relaxed);
            let now = monotonic_ns();
            let mut worst = transport.capture_lag_max_ns.load(Ordering::Relaxed);
            let mut last = 0;
            {
                let mut engine = shared.engine.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                for index in 0..count as usize {
                    let length = headers[index].msg_len as usize;
                    if length < 8 {
                        continue;
                    }
                    let frame = &frames[index];
                    let can_id = u32::from_ne_bytes([frame[0], frame[1], frame[2], frame[3]]);
                    let payload = (frame[4] as usize).min(length - 8).min(64);
                    let stamp = stamp_of(&headers[index].msg_hdr);
                    let t_mono_ns = if stamp != 0 { clock.to_mono_ns(stamp) } else { now };
                    last = now - t_mono_ns;
                    worst = worst.max(last);
                    if can_id & (CAN_ERR_FLAG | CAN_RTR_FLAG) != 0 {
                        engine.stats.frames += 1;
                        engine.stats.malformed_frames += 1;
                        continue;
                    }
                    let extended = can_id & CAN_EFF_FLAG != 0;
                    let id = can_id & if extended { CAN_EFF_MASK } else { CAN_SFF_MASK };
                    engine.on_frame(id, extended, &frame[8..8 + payload], t_mono_ns);
                }
            }
            received = true;
            transport.capture_lag_ns.store(last, Ordering::Relaxed);
            transport.capture_lag_max_ns.store(worst, Ordering::Relaxed);
            if (count as usize) < BATCH {
                break;
            }
        }
    }
    received
}

fn wait(shared: &Shared, total: Duration) {
    let slice = Duration::from_millis(50);
    let mut waited = Duration::ZERO;
    while waited < total && !shared.stop.load(Ordering::Relaxed) {
        std::thread::sleep(slice);
        waited += slice;
    }
}

/// The reader thread body: open, read, and on any error retry with backoff.
pub fn run(shared: Arc<Shared>, interface: String, recv_buffer_bytes: usize) {
    let mut backoff = BACKOFF_START;
    while !shared.stop.load(Ordering::Relaxed) {
        match open(&interface, recv_buffer_bytes, &shared.transport) {
            Some(fd) => {
                let drops_before = shared.transport.socket_drops.load(Ordering::Relaxed);
                if read_until_error(&fd, &shared, drops_before) {
                    backoff = BACKOFF_START;
                }
                if shared.stop.load(Ordering::Relaxed) {
                    break;
                }
                shared.transport.reconnects.fetch_add(1, Ordering::Relaxed);
            }
            None => {
                shared.transport.open_failures.fetch_add(1, Ordering::Relaxed);
            }
        }
        wait(&shared, backoff);
        backoff = (backoff * 2).min(BACKOFF_MAX);
    }
    shared.running.store(false, Ordering::Relaxed);
}
