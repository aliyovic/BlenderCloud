use anyhow::{bail, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, BufRead, BufWriter, Write};
use std::time::Instant;

use crate::operations;

pub(crate) type Matrix = [[f64; 4]; 4];
pub(crate) const MAX_PAYLOAD_BYTES: usize = 64 * 1024 * 1024;
pub(crate) const MAX_OPEN_SOURCES: usize = 16;
const DEFAULT_WORKING_BYTES: usize = 256 * 1024 * 1024;
const MAX_WORKING_BYTES: usize = 1024 * 1024 * 1024;

fn all_classes() -> i32 {
    -1
}
fn payload_limit() -> usize {
    MAX_PAYLOAD_BYTES
}
fn working_limit() -> usize {
    DEFAULT_WORKING_BYTES
}

#[derive(Deserialize)]
pub(crate) struct View {
    pub source: String,
    #[serde(default)]
    pub cache: String,
    pub object: String,
    pub origin: [f64; 3],
    pub matrix: Matrix,
    pub width: f64,
    pub height: f64,
    #[serde(default)]
    pub clip_matrix: Option<Matrix>,
    #[serde(default = "all_classes")]
    pub class_filter: i32,
    #[serde(default)]
    pub class_index: String,
}

#[derive(Deserialize)]
pub(crate) struct PlanRequest {
    #[serde(default)]
    pub views: Vec<View>,
    #[serde(default)]
    pub budget: u64,
    #[serde(default)]
    pub gpu_budget: Option<u64>,
    #[serde(default)]
    pub pixel_error: f64,
    #[serde(default = "working_limit")]
    pub working_budget_bytes: usize,
}

#[derive(Deserialize)]
pub(crate) struct NodeRequest {
    pub source: String,
    #[serde(default)]
    pub cache: String,
    pub stamp: String,
    #[serde(default)]
    pub origin: [f64; 3],
    #[serde(default)]
    pub node: usize,
    #[serde(default)]
    pub file: String,
    #[serde(default)]
    pub source_count: u64,
    #[serde(default)]
    pub node_total_count: u64,
    #[serde(default = "all_classes")]
    pub class_filter: i32,
    #[serde(default)]
    pub cache_origin: [f64; 3],
    #[serde(default)]
    pub has_rgb: bool,
    #[serde(default)]
    pub bounds: [[f64; 3]; 2],
    #[serde(default)]
    pub point_offset: usize,
    #[serde(default = "payload_limit")]
    pub max_payload_bytes: usize,
    #[serde(default = "working_limit")]
    pub working_budget_bytes: usize,
}

#[derive(Clone, Deserialize)]
pub(crate) struct CropSource {
    pub source: String,
    #[serde(default)]
    pub cache: String,
    pub origin: [f64; 3],
    pub bounds: [[f64; 3]; 2],
    #[serde(default)]
    pub clip_matrix: Option<Matrix>,
    #[serde(default)]
    pub to_output: Option<Matrix>,
    #[serde(default)]
    pub stamp: String,
}

#[derive(Deserialize)]
pub(crate) struct CropRequest {
    pub source: String,
    pub output: String,
    #[serde(default)]
    pub origin: [f64; 3],
    #[serde(default)]
    pub stamp: String,
    #[serde(default)]
    pub bounds: [[f64; 3]; 2],
    #[serde(default)]
    pub clip_matrix: Option<Matrix>,
    pub point_limit: u64,
    #[serde(default)]
    pub source_specs: Vec<CropSource>,
    #[serde(default = "working_limit")]
    pub working_budget_bytes: usize,
}

#[derive(Deserialize)]
pub(crate) struct PickSource {
    pub source: String,
    #[serde(default)]
    pub cache: String,
    #[serde(default)]
    pub origin: [f64; 3],
    pub ray_origin: [f64; 3],
    pub ray_direction: [f64; 3],
    pub max_distance: f64,
    #[serde(default = "all_classes")]
    pub class_filter: i32,
    #[serde(default)]
    pub clip_matrix: Option<Matrix>,
}

#[derive(Deserialize)]
pub(crate) struct PickRequest {
    pub pick_sources: Vec<PickSource>,
    #[serde(default)]
    pub pick_radius: f64,
    #[serde(default)]
    pub plane_radius: f64,
    #[serde(default = "working_limit")]
    pub working_budget_bytes: usize,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    Plan(PlanRequest),
    Node(NodeRequest),
    Crop(CropRequest),
    Pick(PickRequest),
}

impl Request {
    fn working_budget(&self) -> usize {
        match self {
            Self::Plan(r) => r.working_budget_bytes,
            Self::Node(r) => r.working_budget_bytes,
            Self::Crop(r) => r.working_budget_bytes,
            Self::Pick(r) => r.working_budget_bytes,
        }
    }
}

pub(crate) fn record_bytes(count: usize) -> Result<usize> {
    count
        .checked_mul(18)
        .ok_or_else(|| anyhow::anyhow!("Point count overflows payload size"))
}

pub(crate) fn page_bounds(
    req: &NodeRequest,
    total: usize,
    decoder_bytes: usize,
) -> Result<(usize, usize)> {
    if !(18..=MAX_PAYLOAD_BYTES).contains(&req.max_payload_bytes) {
        bail!("max_payload_bytes must be 18 to {}", MAX_PAYLOAD_BYTES);
    }
    if req.point_offset > total {
        bail!("point_offset exceeds node point count");
    }
    let available = req.working_budget_bytes.checked_sub(decoder_bytes + 65536)
        .filter(|bytes| *bytes >= 18)
        .ok_or_else(|| anyhow::anyhow!("Node exceeds decoder working budget. Repartition the source or raise working_budget_bytes."))?;
    let count = (total - req.point_offset).min(req.max_payload_bytes.min(available) / 18);
    Ok((req.point_offset, count))
}

pub(crate) fn page_header(
    req: &NodeRequest,
    count: usize,
    total: usize,
    has_rgb: bool,
    bounds: [[f64; 3]; 2],
) -> Value {
    let end = req.point_offset + count;
    json!({"count":count,"total_count":total,"point_offset":req.point_offset,
        "next_offset":(end < total).then_some(end),"has_rgb":has_rgb,
        "stamp":req.stamp,"bounds":bounds})
}

pub(crate) fn serve() -> Result<()> {
    let stdin = io::stdin();
    let mut stdout = BufWriter::new(io::stdout().lock());
    let mut sources = HashMap::new();
    let mut native = HashMap::new();
    for line in stdin.lock().lines() {
        let line = line?;
        let start = Instant::now();
        let mut working_budget = DEFAULT_WORKING_BYTES;
        let result = (|| -> Result<(Value, Vec<u8>)> {
            let request: Request = serde_json::from_str(&line).map_err(|error| {
                anyhow::anyhow!("Unknown streaming request or malformed request: {error}")
            })?;
            working_budget = request.working_budget();
            if !(1024 * 1024..=MAX_WORKING_BYTES).contains(&working_budget) {
                bail!("working_budget_bytes must be 1 MiB to 1 GiB");
            }
            match request {
                Request::Plan(r) => {
                    Ok((operations::plan(&r, &mut sources, &mut native)?, Vec::new()))
                }
                Request::Node(r) if r.cache.is_empty() => operations::node(&r, &mut sources),
                Request::Node(r) => operations::native_node(&r, &mut native),
                Request::Crop(r) => {
                    Ok((operations::crop(&r, &mut sources, &mut native)?, Vec::new()))
                }
                Request::Pick(r) => {
                    Ok((operations::pick(&r, &mut sources, &mut native)?, Vec::new()))
                }
            }
        })();
        let (mut response, payload) = match result {
            Ok((mut value, data)) => {
                value["ok"] = json!(true);
                (value, data)
            }
            Err(error) => (json!({"ok":false,"error":format!("{error:#}")}), Vec::new()),
        };
        response["protocol_version"] = json!(2);
        response["working_budget_bytes"] = json!(working_budget);
        response["payload_bytes"] = json!(payload.len());
        response["elapsed_ms"] = json!(start.elapsed().as_secs_f64() * 1000.0);
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.write_all(&payload)?;
        stdout.flush()?;
    }
    Ok(())
}
