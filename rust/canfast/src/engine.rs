//! Decode, filter and batch CAN frames exactly as the Python pipeline does.
//!
//! Python builds the plan (it owns the DBCs and the catalog) and this module
//! only executes it: bit extraction, scaling, report-by-exception and the
//! `SampleBatch` wire encoding. `tests/test_can_fast.py` holds the two paths
//! to identical output on recorded buses, so any change here that alters a
//! byte has to be made in `src/core` and `src/collectors/can.py` as well.

use std::collections::{HashMap, HashSet};

use serde::Deserialize;

const VT_DOUBLE: u8 = 1;
const VT_INT64: u8 = 2;
const VT_BOOL: u8 = 3;
const VT_FLOAT: u8 = 5;
const VT_UINT: u8 = 6;
const MAX_TRACKED_UNKNOWN_IDS: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Value {
    Int(i64),
    Float(f64),
}

impl Value {
    fn as_f64(self) -> f64 {
        match self {
            Value::Int(v) => v as f64,
            Value::Float(v) => v,
        }
    }
}

#[derive(Deserialize, Clone, Debug)]
pub struct Field {
    pub start: u32,
    pub length: u32,
    pub big_endian: bool,
    pub signed: bool,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Rbe {
    pub deadband: Option<f64>,
    pub min_interval_ns: Option<i64>,
    pub max_interval_ns: Option<i64>,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Signal {
    #[serde(flatten)]
    pub field: Field,
    /// Whether the DBC conversion keeps integers integral (cantools does).
    pub integer: bool,
    pub scale: f64,
    pub offset: f64,
    pub channel_id: u32,
    pub value_type: u8,
    pub encode_scale: f64,
    pub encode_offset: f64,
    pub rbe: Option<Rbe>,
    /// Also handed to Python, before the filter: fusion and timing inputs.
    pub tap: bool,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Mux {
    pub selector: Field,
    pub nodes: HashMap<i64, Node>,
}

#[derive(Deserialize, Clone, Debug, Default)]
pub struct Node {
    pub signals: Vec<Signal>,
    pub muxes: Vec<Mux>,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Message {
    pub frame_id: u32,
    pub extended: bool,
    pub length: usize,
    pub root: Node,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Plan {
    pub registry_seq: u32,
    pub tick_ns: i64,
    pub messages: Vec<Message>,
    /// Frames a DBC defines but nothing consumes: (frame id, extended).
    pub unwanted: Vec<(u32, bool)>,
}

#[derive(Clone, Copy, Debug)]
pub struct Record {
    pub channel_id: u32,
    pub t_mono_ns: i64,
    pub value: Value,
}

pub struct Batch {
    pub payload: Vec<u8>,
    pub epoch_unix_ms: i64,
    pub epoch_mono_ns: i64,
}

#[derive(Default, Clone, Debug)]
pub struct Stats {
    pub frames: u64,
    pub decoded_frames: u64,
    pub unknown_frames: u64,
    pub malformed_frames: u64,
    pub unwanted_frames: u64,
    pub samples: u64,
    pub rbe_suppressed: u64,
    pub encode_failures: u64,
}

struct Sent {
    value: Value,
    t_mono_ns: i64,
}

pub struct Engine {
    registry_seq: u32,
    tick_ns: i64,
    by_frame: HashMap<(u32, bool), Vec<Message>>,
    unwanted: HashSet<(u32, bool)>,
    policies: HashMap<u32, Signal>,
    sent: HashMap<u32, Sent>,
    staged: Vec<Record>,
    taps: Vec<Record>,
    reported_unknown: HashSet<(u32, bool)>,
    new_unknown: Vec<u32>,
    pub stats: Stats,
}

fn collect_policies(node: &Node, into: &mut HashMap<u32, Signal>) {
    for signal in &node.signals {
        into.insert(signal.channel_id, signal.clone());
    }
    for mux in &node.muxes {
        for child in mux.nodes.values() {
            collect_policies(child, into);
        }
    }
}

/// The raw integer of one signal, as bitstruct (and so cantools) reads it.
fn extract(data: &[u8], field: &Field) -> i64 {
    let length = field.length.min(64);
    let mut raw: u64 = 0;
    if field.big_endian {
        // DBC "sawtooth" numbering: `start` is the most significant bit.
        let mut position = field.start as usize;
        for _ in 0..length {
            let bit = (data[position / 8] >> (position % 8)) & 1;
            raw = (raw << 1) | bit as u64;
            if position % 8 == 0 {
                position += 15;
            } else {
                position -= 1;
            }
        }
    } else {
        for index in 0..length as usize {
            let position = field.start as usize + index;
            let bit = (data[position / 8] >> (position % 8)) & 1;
            raw |= (bit as u64) << index;
        }
    }
    if field.signed && length < 64 && (raw >> (length - 1)) & 1 == 1 {
        raw |= u64::MAX << length;
    }
    raw as i64
}

fn fits(field: &Field, length: usize) -> bool {
    if field.length == 0 {
        return false;
    }
    let bits = length * 8;
    if field.big_endian {
        let sequential = 8 * (field.start as usize / 8) + (7 - field.start as usize % 8);
        sequential + field.length as usize <= bits
    } else {
        field.start as usize + field.length as usize <= bits
    }
}

fn node_fits(node: &Node, length: usize) -> bool {
    node.signals.iter().all(|s| fits(&s.field, length))
        && node.muxes.iter().all(|m| {
            fits(&m.selector, length) && m.nodes.values().all(|n| node_fits(n, length))
        })
}

fn scaled(signal: &Signal, raw: i64) -> Value {
    if signal.integer {
        Value::Int(raw.wrapping_mul(signal.scale as i64).wrapping_add(signal.offset as i64))
    } else {
        Value::Float(raw as f64 * signal.scale + signal.offset)
    }
}

/// Decode one node of a message's multiplexer tree; false if a selector
/// carries a value the DBC does not define (cantools raises there).
fn decode_node(node: &Node, data: &[u8], out: &mut Vec<(u32, Value, bool)>) -> bool {
    for signal in &node.signals {
        let value = scaled(signal, extract(data, &signal.field));
        out.push((signal.channel_id, value, signal.tap));
    }
    for mux in &node.muxes {
        let selected = extract(data, &mux.selector);
        match mux.nodes.get(&selected) {
            Some(child) => {
                if !decode_node(child, data, out) {
                    return false;
                }
            }
            None => return false,
        }
    }
    true
}

impl Engine {
    pub fn new(plan: Plan) -> Result<Engine, String> {
        if !(10_000_000..=50_000_000).contains(&plan.tick_ns) {
            return Err("tick must be between 10 and 50 ms".into());
        }
        let mut by_frame: HashMap<(u32, bool), Vec<Message>> = HashMap::new();
        let mut policies = HashMap::new();
        for message in plan.messages {
            if message.length > 64 || !node_fits(&message.root, message.length) {
                return Err(format!(
                    "frame 0x{:X}: a signal does not fit the message",
                    message.frame_id
                ));
            }
            collect_policies(&message.root, &mut policies);
            by_frame.entry((message.frame_id, message.extended)).or_default().push(message);
        }
        let unwanted = plan
            .unwanted
            .into_iter()
            .filter(|key| !by_frame.contains_key(key))
            .collect();
        Ok(Engine {
            registry_seq: plan.registry_seq,
            tick_ns: plan.tick_ns,
            by_frame,
            unwanted,
            policies,
            sent: HashMap::new(),
            staged: Vec::new(),
            taps: Vec::new(),
            reported_unknown: HashSet::new(),
            new_unknown: Vec::new(),
            stats: Stats::default(),
        })
    }

    /// One data frame off the bus, stamped on the monotonic timebase.
    pub fn on_frame(&mut self, frame_id: u32, extended: bool, data: &[u8], t_mono_ns: i64) {
        self.stats.frames += 1;
        let key = (frame_id, extended);
        let Some(candidates) = self.by_frame.get(&key) else {
            if self.unwanted.contains(&key) {
                self.stats.unwanted_frames += 1;
            } else {
                self.stats.unknown_frames += 1;
                if self.reported_unknown.len() < MAX_TRACKED_UNKNOWN_IDS
                    && self.reported_unknown.insert(key)
                {
                    self.new_unknown.push(frame_id);
                }
            }
            return;
        };
        let mut decoded: Vec<(u32, Value, bool)> = Vec::new();
        let mut failures = 0;
        for message in candidates {
            // Excess payload is ignored, a short one is an error: cantools
            // with allow_truncated=False, allow_excess=True.
            if data.len() < message.length {
                failures += 1;
                continue;
            }
            let before = decoded.len();
            if !decode_node(&message.root, &data[..message.length], &mut decoded) {
                decoded.truncate(before);
                failures += 1;
            }
        }
        if failures == candidates.len() {
            self.stats.malformed_frames += 1;
        } else {
            self.stats.decoded_frames += 1;
        }
        self.stats.samples += decoded.len() as u64;
        for (channel_id, value, tap) in decoded {
            let record = Record { channel_id, t_mono_ns, value };
            if tap {
                self.taps.push(record);
            }
            if self.accept(&record) {
                self.staged.push(record);
            }
        }
    }

    /// `core.rbe.RbeFilter.accept`, for the numeric values a bus produces.
    fn accept(&mut self, record: &Record) -> bool {
        let Some(policy) = self.policies.get(&record.channel_id).and_then(|s| s.rbe.as_ref())
        else {
            return true;
        };
        let Some(previous) = self.sent.get(&record.channel_id) else {
            self.sent.insert(
                record.channel_id,
                Sent { value: record.value, t_mono_ns: record.t_mono_ns },
            );
            return true;
        };
        let elapsed = record.t_mono_ns - previous.t_mono_ns;
        let forced = policy.max_interval_ns.is_some_and(|max| elapsed >= max);
        if !forced {
            if policy.min_interval_ns.is_some_and(|min| elapsed < min) {
                self.stats.rbe_suppressed += 1;
                return false;
            }
            if let Some(deadband) = policy.deadband {
                let unchanged = if deadband == 0.0 {
                    record.value.as_f64() == previous.value.as_f64()
                } else {
                    (record.value.as_f64() - previous.value.as_f64()).abs() <= deadband
                };
                if unchanged {
                    self.stats.rbe_suppressed += 1;
                    return false;
                }
            }
        }
        self.sent.insert(
            record.channel_id,
            Sent { value: record.value, t_mono_ns: record.t_mono_ns },
        );
        true
    }

    /// Hand over the pre-filter samples Python asked to see.
    pub fn take_taps(&mut self) -> Vec<Record> {
        std::mem::take(&mut self.taps)
    }

    pub fn take_new_unknown(&mut self) -> Vec<u32> {
        std::mem::take(&mut self.new_unknown)
    }

    /// `agent.pipeline.Pipeline.flush`: partition what is staged into tick
    /// windows anchored on the earliest sample and serialize one batch each.
    /// The wall epoch uses a fresh realtime/monotonic pair, as `SystemClock`.
    pub fn flush(&mut self, realtime_ns: i64, monotonic_ns: i64) -> Vec<Batch> {
        let mut staged = std::mem::take(&mut self.staged);
        if staged.is_empty() {
            return Vec::new();
        }
        staged.sort_by_key(|record| record.t_mono_ns);
        let mut batches = Vec::new();
        let mut index = 0;
        while index < staged.len() {
            let epoch_mono_ns = staged[index].t_mono_ns;
            let wall_ms = (realtime_ns as i128 + epoch_mono_ns as i128 - monotonic_ns as i128)
                as f64
                / 1e6;
            let epoch_unix_ms = wall_ms.round_ties_even() as i64;
            let start = index;
            while index < staged.len() && staged[index].t_mono_ns - epoch_mono_ns < self.tick_ns {
                index += 1;
            }
            match self.encode(&staged[start..index], epoch_unix_ms, epoch_mono_ns) {
                Some(payload) => batches.push(Batch { payload, epoch_unix_ms, epoch_mono_ns }),
                // A value that cannot be encoded costs its tick window, as in
                // the Python batcher.
                None => self.stats.encode_failures += 1,
            }
        }
        batches
    }

    fn encode(&self, records: &[Record], epoch_unix_ms: i64, epoch_mono_ns: i64) -> Option<Vec<u8>> {
        let mut out = Vec::with_capacity(24 + records.len() * 16);
        if self.registry_seq != 0 {
            out.push(0x08);
            varint(&mut out, self.registry_seq as u64);
        }
        if epoch_unix_ms != 0 {
            out.push(0x11);
            out.extend_from_slice(&(epoch_unix_ms as u64).to_le_bytes());
        }
        if epoch_mono_ns != 0 {
            out.push(0x19);
            out.extend_from_slice(&(epoch_mono_ns as u64).to_le_bytes());
        }
        let mut sample = Vec::with_capacity(24);
        for record in records {
            let policy = self.policies.get(&record.channel_id)?;
            sample.clear();
            if record.channel_id != 0 {
                sample.push(0x08);
                varint(&mut sample, record.channel_id as u64);
            }
            let offset_us = (record.t_mono_ns - epoch_mono_ns).div_euclid(1_000);
            if offset_us != 0 {
                sample.push(0x10);
                varint(&mut sample, offset_us as u64);
            }
            encode_value(&mut sample, record.value, policy)?;
            out.push(0x22);
            varint(&mut out, sample.len() as u64);
            out.extend_from_slice(&sample);
        }
        out.push(0x28);
        out.push(1); // format_version
        Some(out)
    }
}

fn varint(out: &mut Vec<u8>, mut value: u64) {
    while value >= 0x80 {
        out.push((value as u8) | 0x80);
        value >>= 7;
    }
    out.push(value as u8);
}

/// `core.batcher._encode_value` for integer and float inputs.
fn encode_value(out: &mut Vec<u8>, value: Value, policy: &Signal) -> Option<()> {
    match policy.value_type {
        VT_DOUBLE => {
            out.push(0x19);
            out.extend_from_slice(&value.as_f64().to_le_bytes());
        }
        VT_FLOAT => {
            out.push(0x3D);
            out.extend_from_slice(&(value.as_f64() as f32).to_le_bytes());
        }
        VT_INT64 | VT_UINT => {
            let wire: i64 = if policy.encode_scale != 0.0 {
                let scaled = (value.as_f64() - policy.encode_offset) / policy.encode_scale;
                if !scaled.is_finite() {
                    return None;
                }
                scaled.round_ties_even() as i64
            } else {
                match value {
                    Value::Int(v) => v,
                    Value::Float(v) if v.is_finite() && v.fract() == 0.0 => v as i64,
                    Value::Float(_) => return None,
                }
            };
            if policy.value_type == VT_INT64 {
                out.push(0x20);
                varint(out, ((wire << 1) ^ (wire >> 63)) as u64);
            } else {
                if wire < 0 {
                    return None;
                }
                out.push(0x40);
                varint(out, wire as u64);
            }
        }
        VT_BOOL => {
            let flag = match value {
                Value::Int(0) => false,
                Value::Int(1) => true,
                Value::Float(v) if v == 0.0 => false,
                Value::Float(v) if v == 1.0 => true,
                _ => return None,
            };
            out.push(0x28);
            out.push(flag as u8);
        }
        _ => return None,
    }
    Some(())
}
