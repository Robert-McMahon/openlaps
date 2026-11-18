//! `openlaps_canfast`: the vehicle agent's CAN ingest path as a Python
//! extension. One thread owns the bus socket and runs the engine; Python
//! calls `drain` once a tick for finished batches and the tapped samples.

mod engine;
mod sock;

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use engine::{Engine, Plan, Value};
use sock::{Shared, Transport};

type Drained = (Vec<(Py<PyBytes>, i64, i64)>, Py<PyBytes>, Vec<u32>);

#[pyclass]
struct CanIngest {
    shared: Arc<Shared>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

#[pymethods]
impl CanIngest {
    /// Build the engine from the JSON plan `collectors.can_fast` produces.
    #[new]
    fn new(plan_json: &str) -> PyResult<Self> {
        let plan: Plan = serde_json::from_str(plan_json)
            .map_err(|error| PyValueError::new_err(format!("bad plan: {error}")))?;
        let engine = Engine::new(plan).map_err(PyValueError::new_err)?;
        Ok(CanIngest {
            shared: Arc::new(Shared {
                engine: Mutex::new(engine),
                transport: Transport::default(),
                stop: AtomicBool::new(false),
                running: AtomicBool::new(false),
            }),
            thread: Mutex::new(None),
        })
    }

    /// Start the reader thread on a SocketCAN interface.
    fn start(&self, interface: &str, recv_buffer_bytes: usize, thread_name: &str) -> PyResult<()> {
        let mut slot = self.thread.lock().unwrap();
        if self.shared.running.load(Ordering::Relaxed) {
            return Ok(());
        }
        if let Some(finished) = slot.take() {
            let _ = finished.join();
        }
        self.shared.stop.store(false, Ordering::Relaxed);
        self.shared.running.store(true, Ordering::Relaxed);
        let shared = Arc::clone(&self.shared);
        let interface = interface.to_owned();
        let handle = std::thread::Builder::new()
            .name(thread_name.chars().take(15).collect())
            .spawn(move || sock::run(shared, interface, recv_buffer_bytes))
            .map_err(|error| {
                self.shared.running.store(false, Ordering::Relaxed);
                PyRuntimeError::new_err(format!("cannot start the reader: {error}"))
            })?;
        *slot = Some(handle);
        Ok(())
    }

    /// Stop the reader thread and wait for it.
    fn stop(&self, py: Python<'_>) {
        self.shared.stop.store(true, Ordering::Relaxed);
        let handle = self.thread.lock().unwrap().take();
        if let Some(handle) = handle {
            py.allow_threads(|| {
                let _ = handle.join();
            });
        }
        self.shared.running.store(false, Ordering::Relaxed);
    }

    fn is_running(&self) -> bool {
        self.shared.running.load(Ordering::Relaxed)
    }

    /// Run one frame through the engine without a socket (tests, replays).
    fn feed(&self, frame_id: u32, extended: bool, data: &[u8], t_mono_ns: i64) {
        let mut engine = self.shared.engine.lock().unwrap_or_else(|p| p.into_inner());
        engine.on_frame(frame_id, extended, data, t_mono_ns);
    }

    /// Finished batches, tapped samples and newly seen unknown frame ids.
    ///
    /// Batches are `(payload, epoch_unix_ms, epoch_mono_ns)`. Taps are packed
    /// little-endian `<IBqd`: channel id, 0 for an integer value or 1 for a
    /// float, monotonic ns, value. The clock pair defaults to now and exists
    /// so a test can pin the wall epoch.
    #[pyo3(signature = (realtime_ns=None, monotonic_ns=None))]
    fn drain(
        &self,
        py: Python<'_>,
        realtime_ns: Option<i64>,
        monotonic_ns: Option<i64>,
    ) -> Drained {
        let (batches, taps, unknown) = {
            let mut engine = self.shared.engine.lock().unwrap_or_else(|p| p.into_inner());
            let realtime = realtime_ns.unwrap_or_else(sock::realtime_ns);
            let monotonic = monotonic_ns.unwrap_or_else(sock::monotonic_ns);
            (engine.flush(realtime, monotonic), engine.take_taps(), engine.take_new_unknown())
        };
        let mut packed = Vec::with_capacity(taps.len() * 21);
        for record in &taps {
            packed.extend_from_slice(&record.channel_id.to_le_bytes());
            let (kind, value) = match record.value {
                Value::Int(v) => (0u8, v as f64),
                Value::Float(v) => (1u8, v),
            };
            packed.push(kind);
            packed.extend_from_slice(&record.t_mono_ns.to_le_bytes());
            packed.extend_from_slice(&value.to_le_bytes());
        }
        let batches = batches
            .into_iter()
            .map(|batch| {
                (PyBytes::new(py, &batch.payload).unbind(), batch.epoch_unix_ms, batch.epoch_mono_ns)
            })
            .collect();
        (batches, PyBytes::new(py, &packed).unbind(), unknown)
    }

    /// Every counter, by name.
    fn stats(&self) -> HashMap<&'static str, f64> {
        let engine = self.shared.engine.lock().unwrap_or_else(|p| p.into_inner()).stats.clone();
        let transport = &self.shared.transport;
        let load = |value: &std::sync::atomic::AtomicU64| value.load(Ordering::Relaxed) as f64;
        HashMap::from([
            ("frames", engine.frames as f64),
            ("decoded_frames", engine.decoded_frames as f64),
            ("unknown_frames", engine.unknown_frames as f64),
            ("malformed_frames", engine.malformed_frames as f64),
            ("unwanted_frames", engine.unwanted_frames as f64),
            ("samples", engine.samples as f64),
            ("rbe_suppressed", engine.rbe_suppressed as f64),
            ("encode_failures", engine.encode_failures as f64),
            ("open_failures", load(&transport.open_failures)),
            ("bus_errors", load(&transport.bus_errors)),
            ("reconnects", load(&transport.reconnects)),
            ("socket_drops", load(&transport.socket_drops)),
            ("recv_buffer_bytes", load(&transport.recv_buffer_bytes)),
            ("clock_steps", load(&transport.clock_steps)),
            ("capture_lag_ms", transport.capture_lag_ns.load(Ordering::Relaxed) as f64 / 1e6),
        ])
    }

    /// The worst capture lag since the last call, then start a new window.
    fn take_capture_lag_max_ms(&self) -> f64 {
        let transport = &self.shared.transport;
        let current = transport.capture_lag_ns.load(Ordering::Relaxed);
        transport.capture_lag_max_ns.swap(current, Ordering::Relaxed) as f64 / 1e6
    }
}

impl Drop for CanIngest {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::Relaxed);
        if let Some(handle) = self.thread.lock().unwrap().take() {
            let _ = handle.join();
        }
    }
}

#[pymodule]
fn openlaps_canfast(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CanIngest>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
