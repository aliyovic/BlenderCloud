use anyhow::{anyhow, bail, Context, Result};
use las::copc::Entry;
use laz::record::{LayeredPointRecordDecompressor, RecordDecompressor};
use laz::DecompressionSelection;
use memmap2::MmapOptions;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::{Instant, UNIX_EPOCH};

type Matrix = [[f64; 4]; 4];

#[derive(Deserialize)]
struct View {
    source: String,
    #[serde(default)] cache: String,
    object: String,
    origin: [f64; 3],
    matrix: Matrix,
    width: f64,
    height: f64,
    #[serde(default)] clip_matrix: Option<Matrix>,
    #[serde(default = "all_classes")] class_filter: i32,
    #[serde(default)] class_index: String,
}

#[derive(Deserialize)]
struct Request {
    op: String,
    #[serde(default)] source: String,
    #[serde(default)] cache: String,
    #[serde(default)] views: Vec<View>,
    #[serde(default)] origin: [f64; 3],
    #[serde(default)] node: usize,
    #[serde(default)] file: String,
    #[serde(default)] source_count: u64,
    #[serde(default)] node_total_count: u64,
    #[serde(default = "all_classes")] class_filter: i32,
    #[serde(default)] cache_origin: [f64; 3],
    #[serde(default)] has_rgb: bool,
    #[serde(default)] budget: u64,
    #[serde(default)] gpu_budget: Option<u64>,
    #[serde(default)] pixel_error: f64,
    #[serde(default)] stamp: String,
    #[serde(default)] output: String,
    #[serde(default)] bounds: [[f64; 3]; 2],
    #[serde(default)] clip_matrix: Option<Matrix>,
    #[serde(default)] point_limit: u64,
    #[serde(default)] source_specs: Vec<CropSource>,
    #[serde(default)] pick_sources: Vec<PickSource>,
    #[serde(default)] pick_radius: f64,
    #[serde(default)] plane_radius: f64,
}

#[derive(Clone, Deserialize)]
struct CropSource {
    source: String,
    #[serde(default)] cache: String,
    origin: [f64; 3],
    bounds: [[f64; 3]; 2],
    #[serde(default)] clip_matrix: Option<Matrix>,
    #[serde(default)] to_output: Option<Matrix>,
    #[serde(default)] stamp: String,
}

#[derive(Deserialize)]
struct PickSource {
    source: String,
    #[serde(default)] cache: String,
    #[serde(default)] origin: [f64; 3],
    ray_origin: [f64; 3],
    ray_direction: [f64; 3],
    max_distance: f64,
    #[serde(default = "all_classes")] class_filter: i32,
    #[serde(default)] clip_matrix: Option<Matrix>,
}

fn all_classes() -> i32 { -1 }

struct Source {
    reader: las::CopcReader<'static, BufReader<File>>,
    node_file: BufReader<File>,
    entries: Vec<Entry>,
    bounds: Vec<[[f64; 3]; 2]>,
    spacing: f64,
    stamp: String,
    class_counts: Option<Vec<Vec<(u8, u32)>>>,
}

#[derive(Serialize, Deserialize)]
struct CopcClassIndex {
    stamp: String,
    counts: Vec<Vec<(u8, u32)>>,
}

struct NativeSource {
    manifest: super::octree::Manifest,
    root: String,
}

fn native_source<'a>(native: &'a mut HashMap<String, NativeSource>, path: &str,
                     cache: &str) -> Result<&'a NativeSource> {
    if !native.contains_key(cache) {
        let manifest: super::octree::Manifest = serde_json::from_reader(
            File::open(Path::new(cache).join("manifest.json"))?)?;
        if manifest.version != 1 || manifest.source != path || manifest.stamp != stamp(path)? {
            bail!("Octree cache is stale. Build it again for this source.");
        }
        native.insert(cache.to_owned(), NativeSource { manifest, root:cache.to_owned() });
    }
    Ok(native.get(cache).unwrap())
}

fn stamp(path: &str) -> Result<String> {
    let metadata = fs::metadata(path)?;
    Ok(format!("{}:{}", metadata.len(), metadata.modified()?.duration_since(UNIX_EPOCH)?.as_nanos()))
}

impl Source {
    fn open(path: &str, stamp: String) -> Result<Self> {
        let reader = las::CopcReader::from_path(path)?;
        let node_file = BufReader::new(File::open(path)?);
        let info = reader.header().copc_info_vlr().context("Missing COPC info")?;
        let file_bounds = reader.header().bounds();
        let mut entries: Vec<_> = reader.hierarchy_entries().filter(|e| e.point_count > 0).collect();
        entries.sort_by_key(|e| (e.key.l, e.key.x, e.key.y, e.key.z));
        let bounds = entries.iter().map(|e| {
            let b = e.key.bounds(&info);
            [[b.min.x.max(file_bounds.min.x), b.min.y.max(file_bounds.min.y), b.min.z.max(file_bounds.min.z)],
             [b.max.x.min(file_bounds.max.x), b.max.y.min(file_bounds.max.y), b.max.z.min(file_bounds.max.z)]]
        }).collect();
        Ok(Self { reader, node_file, entries, bounds, spacing: info.spacing, stamp,
            class_counts: None })
    }

    fn ensure_class_counts(&mut self, cache: &str) -> Result<()> {
        if self.class_counts.is_some() { return Ok(()); }
        let path = Path::new(cache).join("copc_class_counts.json");
        if let Ok(file) = File::open(&path) {
            if let Ok(index) = serde_json::from_reader::<_, CopcClassIndex>(BufReader::new(file)) {
                if index.stamp == self.stamp && index.counts.len() == self.entries.len() {
                    self.class_counts = Some(index.counts);
                    return Ok(());
                }
            }
        }
        let format = self.reader.header().point_format().to_u8()?;
        let items = if (6..=8).contains(&format) {
            Some(self.reader.header().laz_vlr()?.items().clone())
        } else { None };
        let mut counts = Vec::with_capacity(self.entries.len());
        for entry in &self.entries {
            let mut histogram = [0u32; 256];
            if let Some(items) = &items {
                self.node_file.seek(SeekFrom::Start(entry.offset))?;
                let mut decoder = LayeredPointRecordDecompressor::new(&mut self.node_file);
                decoder.set_fields_from(items)?;
                decoder.set_selection(DecompressionSelection::base().decompress_classification());
                let mut record = vec![0u8; decoder.record_size()];
                for _ in 0..entry.point_count {
                    decoder.decompress_next(&mut record)?;
                    histogram[record[16] as usize] += 1;
                }
            } else {
                let block = self.reader.read_entry(entry)?;
                for point in block.points() {
                    histogram[u8::from(point?.classification) as usize] += 1;
                }
            }
            counts.push(histogram.iter().enumerate().filter_map(|(class, count)|
                (*count > 0).then_some((class as u8, *count))).collect());
        }
        if !cache.is_empty() {
            fs::create_dir_all(cache)?;
            let tmp = path.with_extension("json.tmp");
            fs::write(&tmp, serde_json::to_vec(&CopcClassIndex {
                stamp: self.stamp.clone(), counts: counts.clone() })?)?;
            fs::rename(tmp, path)?;
        }
        self.class_counts = Some(counts);
        Ok(())
    }
}

fn source<'a>(sources: &'a mut HashMap<String, Source>, path: &str) -> Result<&'a mut Source> {
    let fingerprint = stamp(path)?;
    if sources.get(path).map(|s| s.stamp.as_str()) != Some(fingerprint.as_str()) {
        sources.insert(path.to_owned(), Source::open(path, fingerprint)?);
    }
    sources.get_mut(path).ok_or_else(|| anyhow!("Source was not opened"))
}

fn transform(matrix: &Matrix, p: [f64; 3]) -> [f64; 4] {
    matrix.map(|row| row[0] * p[0] + row[1] * p[1] + row[2] * p[2] + row[3])
}

fn projected_error(view: &View, bounds: [[f64; 3]; 2], spacing: f64) -> Option<f64> {
    if let Some(section) = &view.clip_matrix {
        let mut outside = [true; 6];
        for x in [bounds[0][0], bounds[1][0]] {
            for y in [bounds[0][1], bounds[1][1]] {
                for z in [bounds[0][2], bounds[1][2]] {
                    let p = transform(section, [x-view.origin[0], y-view.origin[1], z-view.origin[2]]);
                    let distances = [p[3]+p[0], p[3]-p[0], p[3]+p[1], p[3]-p[1], p[3]+p[2], p[3]-p[2]];
                    for i in 0..6 { outside[i] &= distances[i] < 0.0; }
                }
            }
        }
        if outside.into_iter().any(|value| value) { return None; }
    }
    let mut outside = [true; 6];
    let mut nearest_w = f64::INFINITY;
    for x in [bounds[0][0], bounds[1][0]] {
        for y in [bounds[0][1], bounds[1][1]] {
            for z in [bounds[0][2], bounds[1][2]] {
                let p = transform(&view.matrix, [x-view.origin[0], y-view.origin[1], z-view.origin[2]]);
                let distances = [p[3]+p[0], p[3]-p[0], p[3]+p[1], p[3]-p[1], p[3]+p[2], p[3]-p[2]];
                for i in 0..6 { outside[i] &= distances[i] < 0.0; }
                nearest_w = nearest_w.min(p[3]);
            }
        }
    }
    if outside.into_iter().any(|x| x) { return None; }
    let row_length = |i: usize| view.matrix[i][..3].iter().map(|v| v*v).sum::<f64>().sqrt();
    let scale = (row_length(0) * view.width * 0.5).max(row_length(1) * view.height * 0.5);
    Some(spacing * scale / nearest_w.max(0.01))
}

fn plan(req: &Request, sources: &mut HashMap<String, Source>,
        native: &mut HashMap<String, NativeSource>) -> Result<Value> {
    let pixel_error = req.pixel_error.max(0.25);
    let mut candidates = Vec::new();
    let mut considered = 0;
    for view in &req.views {
        if view.cache.is_empty() {
            let src = source(sources, &view.source)?;
            if view.class_filter >= 0 { src.ensure_class_counts(&view.class_index)?; }
            for (id, entry) in src.entries.iter().enumerate() {
                considered += 1;
                let filtered_count = if view.class_filter < 0 { entry.point_count as u64 } else {
                    src.class_counts.as_ref().unwrap()[id].iter()
                        .find(|(class, _)| i32::from(*class) == view.class_filter)
                        .map_or(0, |(_, count)| u64::from(*count))
                };
                if filtered_count == 0 { continue; }
                let spacing = src.spacing / 2_f64.powi(entry.key.l)
                    * (entry.point_count as f64 / filtered_count as f64).sqrt();
                let error = match projected_error(view, src.bounds[id], spacing) {
                    Some(error) => error,
                    None => continue,
                };
                if entry.key.l > 0 && error * 2.0 < pixel_error { continue; }
                candidates.push((error, view.source.clone(), String::new(), src.stamp.clone(), id,
                    [entry.key.l,entry.key.x,entry.key.y,entry.key.z], filtered_count,
                    src.bounds[id], view.origin, view.object.clone(), view.class_filter));
            }
        } else {
            let src = native_source(native, &view.source, &view.cache)?;
            for (id, entry) in src.manifest.nodes.iter().enumerate() {
                considered += 1;
                let filter = if entry.class_counts.is_empty() { -1 } else { view.class_filter };
                let filtered_count = if filter < 0 { entry.count } else {
                    entry.class_counts.iter().find(|(class, _)| i32::from(*class) == filter)
                        .map_or(0, |(_, count)| *count)
                };
                if filtered_count == 0 { continue; }
                let spacing = entry.spacing * (entry.count as f64 / filtered_count as f64).sqrt();
                let error = match projected_error(view, entry.bounds, spacing) {
                    Some(error) => error,
                    None => continue,
                };
                if entry.key[0] > 0 && error * 2.0 < pixel_error { continue; }
                candidates.push((error, view.source.clone(), view.cache.clone(),
                    src.manifest.stamp.clone(), id, entry.key.map(|v| v as i32), filtered_count,
                    entry.bounds, view.origin, view.object.clone(), filter));
            }
        }
    }
    candidates.sort_by(|a,b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(&b.1)).then(a.4.cmp(&b.4)));
    let mut remaining = req.budget.min(50_000_000);
    let mut gpu_remaining = req.gpu_budget.unwrap_or(50_000_000).min(50_000_000);
    let mut nodes = Vec::new();
    for (error, path, cache, fingerprint, id, key, source_count, bounds, origin, object, class_filter) in candidates {
        if remaining == 0 { break; }
        if source_count > gpu_remaining { continue; }
        let count = source_count.min(remaining);
        remaining -= count;
        gpu_remaining -= source_count;
        let drawn_error = error * (source_count as f64 / count as f64).sqrt();
        let (file, cache_origin, has_rgb, total_count) = if cache.is_empty() {
            (String::new(), [0.0;3], false, source_count)
        } else {
            let src = native_source(native, &path, &cache)?;
            (src.manifest.nodes[id].file.clone(), src.manifest.origin, src.manifest.has_rgb,
                src.manifest.nodes[id].count)
        };
        nodes.push(json!({"source":path,"cache":cache,"stamp":fingerprint,"node":id,
            "key":key,
            "source_count":source_count,"count":count,"error":drawn_error,
            "bounds":bounds,"origin":origin,"object":object,
            "file":file,"cache_origin":cache_origin,"has_rgb":has_rgb,
            "total_count":total_count,"class_filter":class_filter}));
    }
    Ok(json!({"nodes":nodes,"points":req.budget.min(50_000_000)-remaining,"considered":considered}))
}

fn node(req: &Request, sources: &mut HashMap<String, Source>) -> Result<(Value, Vec<u8>)> {
    let src = source(sources, &req.source)?;
    if src.stamp != req.stamp { bail!("Source changed. Refresh the view to reopen it."); }
    let entry = *src.entries.get(req.node).context("Unknown COPC node")?;
    if req.class_filter >= 0 {
        let mut selected = Vec::new();
        let format = src.reader.header().point_format().to_u8()?;
        if (6..=8).contains(&format) {
            let transforms = *src.reader.header().transforms();
            let items = src.reader.header().laz_vlr()?.items().clone();
            src.node_file.seek(SeekFrom::Start(entry.offset))?;
            let mut decoder = LayeredPointRecordDecompressor::new(&mut src.node_file);
            decoder.set_fields_from(&items)?;
            let mut selection = DecompressionSelection::base().decompress_z()
                .decompress_classification().decompress_intensity();
            if format >= 7 { selection = selection.decompress_rgb(); }
            decoder.set_selection(selection);
            let mut record = vec![0u8; decoder.record_size()];
            for _ in 0..entry.point_count {
                decoder.decompress_next(&mut record)?;
                if i32::from(record[16]) != req.class_filter { continue; }
                let xyz: [f32; 3] = std::array::from_fn(|axis| {
                    let raw = i32::from_le_bytes(record[axis*4..axis*4+4].try_into().unwrap());
                    let transform = [&transforms.x, &transforms.y, &transforms.z][axis];
                    ((raw as f64 * transform.scale + transform.offset) - req.origin[axis]) as f32
                });
                let rgb = if format >= 7 {
                    [record[31], record[33], record[35]]
                } else { [220; 3] };
                let intensity = u16::from_le_bytes(record[12..14].try_into()?);
                selected.push((xyz, rgb, intensity));
            }
        } else {
            let block = src.reader.read_entry(&entry)?;
            for point in block.points() {
                let point = point?;
                if i32::from(u8::from(point.classification)) != req.class_filter { continue; }
                let world = [point.x, point.y, point.z];
                let xyz = std::array::from_fn(|axis| (world[axis] - req.origin[axis]) as f32);
                let rgb = point.color.map(|c| [(c.red>>8) as u8,
                    (c.green>>8) as u8, (c.blue>>8) as u8]).unwrap_or([220;3]);
                selected.push((xyz, rgb, point.intensity));
            }
        }
        let count = selected.len();
        if req.source_count > 0 && count != usize::try_from(req.source_count)? {
            bail!("COPC class index is stale");
        }
        let mut rng = (entry.offset ^ count as u64).wrapping_add(0x9e3779b97f4a7c15);
        for i in (1..count).rev() {
            rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
            selected.swap(i, rng as usize % (i+1));
        }
        let mut bytes = vec![0u8; count * 18];
        for (dst, (xyz, rgb, intensity)) in selected.into_iter().enumerate() {
            for axis in 0..3 {
                bytes[dst*12+axis*4..dst*12+axis*4+4]
                    .copy_from_slice(&xyz[axis].to_le_bytes());
            }
            bytes[count*12+dst*3..count*12+dst*3+3].copy_from_slice(&rgb);
            bytes[count*15+dst*2..count*15+dst*2+2].copy_from_slice(&intensity.to_le_bytes());
            bytes[count*17+dst] = req.class_filter as u8;
        }
        return Ok((json!({"count":count,
            "has_rgb":src.reader.header().point_format().has_color,
            "stamp":src.stamp,"bounds":src.bounds[req.node]}), bytes));
    }
    let total = usize::try_from(entry.point_count)?;
    let count = total;
    let mut order: Vec<usize> = (0..total).collect();
    let mut rng = (entry.offset ^ total as u64).wrapping_add(0x9e3779b97f4a7c15);
    for i in (1..total).rev() {
        rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
        order.swap(i, rng as usize % (i+1));
    }
    let mut destination = vec![0usize; total];
    for (i, index) in order.into_iter().enumerate() { destination[index] = i; }
    let mut bytes = vec![0u8; count * 18];
    let format = src.reader.header().point_format().to_u8()?;
    if (6..=8).contains(&format) {
        let transforms = *src.reader.header().transforms();
        let items = src.reader.header().laz_vlr()?.items().clone();
        src.node_file.seek(SeekFrom::Start(entry.offset))?;
        let mut decoder = LayeredPointRecordDecompressor::new(&mut src.node_file);
        decoder.set_fields_from(&items)?;
        let mut selection = DecompressionSelection::base().decompress_z()
            .decompress_classification().decompress_intensity();
        if format >= 7 { selection = selection.decompress_rgb(); }
        decoder.set_selection(selection);
        let mut record = vec![0u8; decoder.record_size()];
        for index in 0..total {
            decoder.decompress_next(&mut record)?;
            let dst = destination[index];
            for axis in 0..3 {
                let raw = i32::from_le_bytes(record[axis*4..axis*4+4].try_into()?);
                let transform = [&transforms.x, &transforms.y, &transforms.z][axis];
                let coordinate = raw as f64 * transform.scale + transform.offset;
                let offset = dst * 12 + axis * 4;
                bytes[offset..offset+4].copy_from_slice(&((coordinate-req.origin[axis]) as f32).to_le_bytes());
            }
            let rgb = if format >= 7 {
                [record[31],record[33],record[35]]
            } else { [220;3] };
            bytes[count*12+dst*3..count*12+dst*3+3].copy_from_slice(&rgb);
            bytes[count*15+dst*2..count*15+dst*2+2].copy_from_slice(&record[12..14]);
            bytes[count*17+dst] = record[16];
        }
    } else {
        let block = src.reader.read_entry(&entry)?;
        for (index, p) in block.points().enumerate() {
            let dst = destination[index];
            let p = p?;
            for (axis, coordinate) in [p.x,p.y,p.z].into_iter().enumerate() {
                let offset = dst * 12 + axis * 4;
                bytes[offset..offset+4].copy_from_slice(&((coordinate-req.origin[axis]) as f32).to_le_bytes());
            }
            let rgb = p.color.map(|c| [(c.red>>8) as u8,(c.green>>8) as u8,(c.blue>>8) as u8]).unwrap_or([220;3]);
            bytes[count*12+dst*3..count*12+dst*3+3].copy_from_slice(&rgb);
            bytes[count*15+dst*2..count*15+dst*2+2].copy_from_slice(&p.intensity.to_le_bytes());
            bytes[count*17+dst] = u8::from(p.classification);
        }
    }
    Ok((json!({"count":count,"has_rgb":src.reader.header().point_format().has_color,
        "stamp":src.stamp,"bounds":src.bounds[req.node]}), bytes))
}

fn native_node(req: &Request, native: &mut HashMap<String, NativeSource>) -> Result<(Value, Vec<u8>)> {
    let (file_name, count, total_count, cache_origin, bounds, has_rgb) = if !req.file.is_empty() {
        if stamp(&req.source)? != req.stamp { bail!("Source changed. Rebuild the octree cache."); }
        let name = req.file.strip_prefix("nodes/").context("Invalid octree node path")?;
        if name.contains('/') || name.contains("..") { bail!("Invalid octree node path"); }
        (req.file.clone(), usize::try_from(req.source_count)?,
            usize::try_from(req.node_total_count)?, req.cache_origin, req.bounds, req.has_rgb)
    } else {
        let src = native_source(native, &req.source, &req.cache)?;
        if src.manifest.stamp != req.stamp { bail!("Source changed. Rebuild the octree cache."); }
        let entry = src.manifest.nodes.get(req.node).context("Unknown octree node")?;
        let count = if req.class_filter < 0 { entry.count } else {
            entry.class_counts.iter().find(|(class, _)| i32::from(*class) == req.class_filter)
                .map_or(0, |(_, count)| *count)
        };
        (entry.file.clone(), usize::try_from(count)?, usize::try_from(entry.count)?,
            src.manifest.origin, entry.bounds, src.manifest.has_rgb)
    };
    let file = File::open(Path::new(&req.cache).join(file_name))?;
    let data = unsafe { MmapOptions::new().map(&file)? };
    if data.len() != total_count * 18 { bail!("Incomplete octree node"); }
    let mut bytes = vec![0u8; count * 18];
    let mut destination = 0;
    for point in data.chunks_exact(18) {
        if req.class_filter >= 0 && i32::from(point[17]) != req.class_filter { continue; }
        for axis in 0..3 {
            let value = f32::from_le_bytes(point[axis*4..axis*4+4].try_into()?);
            let local = (f64::from(value) + cache_origin[axis] - req.origin[axis]) as f32;
            bytes[destination*12+axis*4..destination*12+axis*4+4].copy_from_slice(&local.to_le_bytes());
        }
        bytes[count*12+destination*3..count*12+destination*3+3].copy_from_slice(&point[12..15]);
        bytes[count*15+destination*2..count*15+destination*2+2].copy_from_slice(&point[15..17]);
        bytes[count*17+destination] = point[17];
        destination += 1;
    }
    if destination != count { bail!("Octree class counts are stale"); }
    Ok((json!({"count":count,"has_rgb":has_rgb,
        "stamp":req.stamp,"bounds":bounds}), bytes))
}

fn intersects(a: [[f64;3];2], b: [[f64;3];2]) -> bool {
    (0..3).all(|i| a[0][i] <= b[1][i] && a[1][i] >= b[0][i])
}

fn crop_point(req: &Request, spec: &CropSource, xyz: [f64; 3], rgb: [u8; 3],
              intensity: f32, class: u8) -> Option<super::Point> {
    if !(0..3).all(|i| xyz[i]>=spec.bounds[0][i] && xyz[i]<=spec.bounds[1][i]) { return None; }
    let local = std::array::from_fn(|i| xyz[i]-spec.origin[i]);
    if let Some(matrix) = &spec.clip_matrix {
        let q = transform(matrix, local);
        if q[..3].iter().any(|v| v.abs() > 1.0 + 1e-9) { return None; }
    }
    let output_xyz = if let Some(matrix) = &spec.to_output {
        let q = transform(matrix, local);
        std::array::from_fn(|i| req.origin[i] + q[i]/q[3])
    } else { xyz };
    Some(super::Point { xyz:output_xyz, rgb, intensity, class })
}

fn write_crop_point(req: &Request, source_index: usize, node_id: usize, point_index: usize,
                    point: super::Point, out: &mut super::CacheWriter,
                    ids: &mut BufWriter<File>) -> Result<()> {
    if out.retained >= req.point_limit { bail!("Crop exceeds {} points. Shrink the modeling box or raise the crop limit.",req.point_limit); }
    out.push(point)?;
    let node_id = ((source_index as u64) << 24) | node_id as u64;
    ids.write_all(&((node_id << 32) | point_index as u64).to_le_bytes())?;
    Ok(())
}

fn append_crop_point(req: &Request, spec: &CropSource, source_index: usize, node_id: usize,
                     point_index: usize, xyz: [f64; 3], rgb: [u8; 3], intensity: f32, class: u8,
                     out: &mut super::CacheWriter, ids: &mut BufWriter<File>) -> Result<bool> {
    let Some(point) = crop_point(req, spec, xyz, rgb, intensity, class) else { return Ok(false); };
    write_crop_point(req, source_index, node_id, point_index, point, out, ids)?;
    Ok(true)
}

fn crop(req: &Request, sources: &mut HashMap<String, Source>,
        native: &mut HashMap<String, NativeSource>) -> Result<Value> {
    if req.point_limit == 0 || req.point_limit > 5_000_000 { bail!("Crop point limit must be 1 to 5,000,000"); }
    let source_specs = if req.source_specs.is_empty() {
        vec![CropSource { source:req.source.clone(), origin:req.origin, bounds:req.bounds,
            cache:String::new(), clip_matrix:req.clip_matrix, to_output:None, stamp:req.stamp.clone() }]
    } else { req.source_specs.clone() };
    if source_specs.len() > 256 { bail!("A crop can use at most 256 sources"); }
    for spec in &source_specs {
        if !(0..3).all(|i| spec.bounds[0][i].is_finite() && spec.bounds[1][i].is_finite()
            && spec.bounds[0][i] < spec.bounds[1][i]) { bail!("Invalid crop bounds"); }
    }
    let output = Path::new(&req.output);
    if output.exists() { bail!("Crop output already exists"); }
    let result = (|| -> Result<Value> {
        let mut out = super::CacheWriter::new(output, 0, req.point_limit)?;
        out.origin = Some(req.origin);
        let mut ids = BufWriter::new(File::create(output.join("source_ids.u64"))?);
        let mut node_keys = Vec::new();
        let mut decoded = 0u64;
        let mut source_points = 0;
        let mut has_rgb = false;
        let mut source_stamps = Vec::new();
        for (source_index, spec) in source_specs.iter().enumerate() {
            if spec.cache.is_empty() {
                let src = source(sources, &spec.source)?;
                if !spec.stamp.is_empty() && spec.stamp != src.stamp { bail!("Source changed before crop"); }
                source_points += src.reader.header().number_of_points();
                has_rgb |= src.reader.header().point_format().has_color;
                source_stamps.push(json!({"source":spec.source,"stamp":src.stamp}));
                let work: Vec<(usize, Entry)> = src.entries.iter().copied().enumerate()
                    .filter(|(id, _)| intersects(src.bounds[*id], spec.bounds)).collect();
                if work.iter().any(|(id, _)| *id > 0x00ff_ffff) {
                    bail!("Too many nodes in crop source");
                }
                let pool = rayon::ThreadPoolBuilder::new().num_threads(6).build()?;
                for chunk in work.chunks(6) {
                    let results: Vec<Result<(usize, Entry, u64, Vec<(usize, super::Point)>)>> =
                        pool.install(|| chunk.par_iter().map_init(
                            || las::CopcReader::from_path(&spec.source),
                            |reader_result, &(id, entry)| -> Result<_> {
                                let reader = reader_result.as_mut().map_err(|error| anyhow!("{error}"))?;
                                let block = reader.read_entry(&entry)?;
                                let decoded_count = block.len() as u64;
                                let mut selected = Vec::new();
                                for (index, point) in block.points().enumerate() {
                                    let p = point?;
                                    let rgb = p.color.map(|c| [(c.red>>8) as u8,
                                        (c.green>>8) as u8, (c.blue>>8) as u8]).unwrap_or([220;3]);
                                    if let Some(point) = crop_point(req, spec, [p.x,p.y,p.z], rgb,
                                            p.intensity as f32 / 65535.0,
                                            u8::from(p.classification)) {
                                        selected.push((index, point));
                                        if selected.len() as u64 > req.point_limit {
                                            bail!("Crop exceeds {} points. Shrink the modeling box or raise the crop limit.",req.point_limit);
                                        }
                                    }
                                }
                                Ok((id, entry, decoded_count, selected))
                            }).collect());
                    for result in results {
                        let (id, entry, decoded_count, selected) = result?;
                        decoded += decoded_count;
                        let included = !selected.is_empty();
                        for (point_index, point) in selected {
                            write_crop_point(req, source_index, id, point_index, point,
                                             &mut out, &mut ids)?;
                        }
                        if included { node_keys.push(json!({"source_index":source_index,"id":id,
                            "key":[entry.key.l,entry.key.x,entry.key.y,entry.key.z]})); }
                    }
                }
            } else {
                let src = native_source(native, &spec.source, &spec.cache)?;
                if !spec.stamp.is_empty() && spec.stamp != src.manifest.stamp { bail!("Source changed before crop"); }
                source_points += src.manifest.source_points;
                has_rgb |= src.manifest.has_rgb;
                source_stamps.push(json!({"source":spec.source,"stamp":src.manifest.stamp,"cache":spec.cache}));
                for (id, entry) in src.manifest.nodes.iter().enumerate() {
                    if !intersects(entry.bounds, spec.bounds) { continue; }
                    if id > 0x00ff_ffff { bail!("Too many nodes in crop source"); }
                    let file = File::open(Path::new(&src.root).join(&entry.file))?;
                    let data = unsafe { MmapOptions::new().map(&file)? };
                    if data.len() != usize::try_from(entry.count)? * 18 { bail!("Incomplete octree node"); }
                    decoded += entry.count;
                    let mut included = false;
                    for (index, record) in data.chunks_exact(18).enumerate() {
                        let xyz = std::array::from_fn(|axis| {
                            let raw = f32::from_le_bytes(record[axis*4..axis*4+4].try_into().unwrap());
                            f64::from(raw) + src.manifest.origin[axis]
                        });
                        let rgb = record[12..15].try_into()?;
                        let intensity = f32::from(u16::from_le_bytes(record[15..17].try_into()?)) / 65535.0;
                        included |= append_crop_point(req, spec, source_index, id, index, xyz, rgb,
                            intensity, record[17], &mut out, &mut ids)?;
                    }
                    if included { node_keys.push(json!({"source_index":source_index,"id":id,"key":entry.key})); }
                }
            }
        }
        ids.flush()?;
        let manifest = out.finish(Path::new(&req.source), source_points, [has_rgb,true,true])?;
        let mut value = serde_json::to_value(manifest)?;
        value["full_resolution_crop"] = json!(true);
        value["source_stamp"] = json!(source_stamps[0]["stamp"]);
        value["source_sources"] = json!(source_stamps);
        value["source_id_encoding"] = json!("source index in bits 56-63, node index in bits 32-55, point index in bits 0-31");
        value["source_nodes"] = json!(node_keys);
        value["crop_bounds"] = json!(req.bounds);
        fs::write(output.join("manifest.json"), serde_json::to_vec_pretty(&value)?)?;
        Ok(json!({"output":req.output,"manifest":value,"decoded_points":decoded}))
    })();
    if result.is_err() { let _ = fs::remove_dir_all(output); }
    result
}

fn ray_meets_bounds(origin: [f64; 3], direction: [f64; 3], bounds: [[f64; 3]; 2], radius: f64,
                    max_distance: f64) -> bool {
    let (mut near, mut far) = (0.0f64, max_distance);
    for axis in 0..3 {
        let low = bounds[0][axis] - radius;
        let high = bounds[1][axis] + radius;
        if direction[axis].abs() < 1e-12 {
            if origin[axis] < low || origin[axis] > high { return false; }
        } else {
            let a = (low - origin[axis]) / direction[axis];
            let b = (high - origin[axis]) / direction[axis];
            near = near.max(a.min(b));
            far = far.min(a.max(b));
            if far < near { return false; }
        }
    }
    true
}

fn fitted_normal(points: &[[f64; 3]], ray: [f64; 3]) -> Option<[f64; 3]> {
    if points.len() < 3 { return None; }
    let mut center = [0.0; 3];
    for point in points { for axis in 0..3 { center[axis] += point[axis]; } }
    for value in &mut center { *value /= points.len() as f64; }
    let mut covariance = [[0.0; 3]; 3];
    for point in points {
        for i in 0..3 { for j in 0..3 {
            covariance[i][j] += (point[i] - center[i]) * (point[j] - center[j]);
        } }
    }
    let mut vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    for _ in 0..24 {
        let (mut p, mut q) = (0, 1);
        for (i, j) in [(0, 1), (0, 2), (1, 2)] {
            if covariance[i][j].abs() > covariance[p][q].abs() { (p, q) = (i, j); }
        }
        if covariance[p][q].abs() < 1e-12 { break; }
        let angle = 0.5 * (2.0 * covariance[p][q]).atan2(covariance[q][q] - covariance[p][p]);
        let (s, c) = angle.sin_cos();
        for row in 0..3 {
            let (a, b) = (covariance[row][p], covariance[row][q]);
            covariance[row][p] = c * a - s * b;
            covariance[row][q] = s * a + c * b;
            let (a, b) = (vectors[row][p], vectors[row][q]);
            vectors[row][p] = c * a - s * b;
            vectors[row][q] = s * a + c * b;
        }
        for column in 0..3 {
            let (a, b) = (covariance[p][column], covariance[q][column]);
            covariance[p][column] = c * a - s * b;
            covariance[q][column] = s * a + c * b;
        }
    }
    let axis = (0..3).min_by(|a, b| covariance[*a][*a].total_cmp(&covariance[*b][*b]))?;
    let mut normal = [vectors[0][axis], vectors[1][axis], vectors[2][axis]];
    let length = normal.iter().map(|v| v*v).sum::<f64>().sqrt();
    if length < 1e-12 { return None; }
    for value in &mut normal { *value /= length; }
    if (0..3).map(|i| normal[i]*ray[i]).sum::<f64>() > 0.0 {
        for value in &mut normal { *value = -*value; }
    }
    Some(normal)
}

fn consider_pick(spec: &PickSource, source_index: usize, node_id: usize, point_index: usize,
                 xyz: [f64; 3], class: u8, direction: [f64; 3], radius: f64, plane_radius: f64,
                 neighbors: &mut Vec<(usize, [f64; 3])>,
                 best: &mut Option<(f64, f64, usize, [f64; 3], u64)>) {
    if spec.class_filter >= 0 && i32::from(class) != spec.class_filter { return; }
    if let Some(matrix) = &spec.clip_matrix {
        let local = std::array::from_fn(|i| xyz[i] - spec.origin[i]);
        let q = transform(matrix, local);
        if q[..3].iter().any(|v| v.abs() > 1.0 + 1e-9) { return; }
    }
    let delta = std::array::from_fn::<_, 3, _>(|i| xyz[i] - spec.ray_origin[i]);
    let distance = (0..3).map(|i| delta[i]*direction[i]).sum::<f64>();
    if distance < 0.0 || distance > spec.max_distance { return; }
    let offset = std::array::from_fn::<_, 3, _>(|i| delta[i] - distance*direction[i]);
    let squared = offset.iter().map(|v| v*v).sum::<f64>();
    if squared > plane_radius*plane_radius { return; }
    neighbors.push((source_index, xyz));
    if squared > radius*radius { return; }
    let id = ((source_index as u64) << 56) | ((node_id as u64) << 32) | point_index as u64;
    if best.as_ref().is_none_or(|(best_squared, best_distance, _, _, _)|
        squared < *best_squared || (squared == *best_squared && distance < *best_distance)) {
        *best = Some((squared, distance, source_index, xyz, id));
    }
}

fn pick(req: &Request, sources: &mut HashMap<String, Source>,
        native: &mut HashMap<String, NativeSource>) -> Result<Value> {
    if req.pick_sources.is_empty() { bail!("Pick needs at least one source"); }
    if req.pick_sources.len() > 256 { bail!("Pick supports at most 256 sources"); }
    let radius = req.pick_radius.clamp(0.001, 100.0);
    let plane_radius = req.plane_radius.clamp(radius, 100.0);
    let mut neighbors = Vec::new();
    let mut best: Option<(f64, f64, usize, [f64; 3], u64)> = None;
    let mut decoded = 0u64;
    for (source_index, spec) in req.pick_sources.iter().enumerate() {
        let direction_length = spec.ray_direction.iter().map(|v| v*v).sum::<f64>().sqrt();
        if direction_length < 1e-12 || !spec.max_distance.is_finite() || spec.max_distance <= 0.0 {
            bail!("Invalid pick ray");
        }
        let direction = spec.ray_direction.map(|v| v / direction_length);
        if spec.cache.is_empty() {
            let src = source(sources, &spec.source)?;
            for (node_id, entry) in src.entries.iter().enumerate() {
                if !ray_meets_bounds(spec.ray_origin, direction, src.bounds[node_id], plane_radius,
                                     spec.max_distance) { continue; }
                let block = src.reader.read_entry(entry)?;
                decoded += block.len() as u64;
                for (point_index, point) in block.points().enumerate() {
                    let point = point?;
                    consider_pick(spec, source_index, node_id, point_index,
                        [point.x, point.y, point.z], u8::from(point.classification), direction,
                        radius, plane_radius, &mut neighbors, &mut best);
                }
            }
        } else {
            let src = native_source(native, &spec.source, &spec.cache)?;
            for (node_id, entry) in src.manifest.nodes.iter().enumerate() {
                if !ray_meets_bounds(spec.ray_origin, direction, entry.bounds, plane_radius,
                                     spec.max_distance) { continue; }
                let file = File::open(Path::new(&src.root).join(&entry.file))?;
                let data = unsafe { MmapOptions::new().map(&file)? };
                if data.len() != usize::try_from(entry.count)? * 18 { bail!("Incomplete octree node"); }
                decoded += entry.count;
                for (point_index, record) in data.chunks_exact(18).enumerate() {
                    let xyz = std::array::from_fn(|axis| {
                        let raw = f32::from_le_bytes(record[axis*4..axis*4+4].try_into().unwrap());
                        f64::from(raw) + src.manifest.origin[axis]
                    });
                    consider_pick(spec, source_index, node_id, point_index, xyz, record[17], direction,
                        radius, plane_radius, &mut neighbors, &mut best);
                }
            }
        }
    }
    let Some((squared, _distance, source_index, xyz, id)) = best else {
        return Ok(json!({"found":false,"decoded_points":decoded}));
    };
    let plane_points: Vec<_> = neighbors.into_iter().filter_map(|(index, point)| {
        let dist = (0..3).map(|i| (point[i]-xyz[i]).powi(2)).sum::<f64>();
        (index == source_index && dist <= plane_radius*plane_radius).then_some(point)
    }).collect();
    let normal = fitted_normal(&plane_points, req.pick_sources[source_index].ray_direction);
    Ok(json!({"found":true,"source":req.pick_sources[source_index].source,
        "source_index":source_index,"source_id":id,"xyz":xyz,"normal":normal,
        "plane_points":plane_points.len(),"ray_distance":squared.sqrt(),"decoded_points":decoded}))
}

pub fn serve() -> Result<()> {
    let stdin = io::stdin();
    let mut stdout = BufWriter::new(io::stdout().lock());
    let mut sources = HashMap::new();
    let mut native = HashMap::new();
    for line in stdin.lock().lines() {
        let line = line?;
        let start = Instant::now();
        let result = (|| -> Result<(Value,Vec<u8>)> {
            let request: Request = serde_json::from_str(&line)?;
            match request.op.as_str() {
                "plan" => Ok((plan(&request,&mut sources,&mut native)?,Vec::new())),
                "node" => if request.cache.is_empty() {
                    node(&request,&mut sources)
                } else { native_node(&request,&mut native) },
                "crop" => Ok((crop(&request,&mut sources,&mut native)?,Vec::new())),
                "pick" => Ok((pick(&request,&mut sources,&mut native)?,Vec::new())),
                _ => bail!("Unknown streaming request"),
            }
        })();
        let (mut response, payload) = match result {
            Ok((mut value, data)) => { value["ok"] = json!(true); (value,data) },
            Err(error) => (json!({"ok":false,"error":format!("{error:#}")}),Vec::new()),
        };
        response["payload_bytes"] = json!(payload.len());
        response["elapsed_ms"] = json!(start.elapsed().as_secs_f64()*1000.0);
        serde_json::to_writer(&mut stdout,&response)?;
        stdout.write_all(b"\n")?;
        stdout.write_all(&payload)?;
        stdout.flush()?;
    }
    Ok(())
}
