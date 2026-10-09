use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use super::{Point, PointSink};

#[derive(Deserialize)]
struct Preview {
    origin: [f64; 3],
    bounds_min: [f64; 3],
    bounds_max: [f64; 3],
    source_points: u64,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Node {
    pub key: [u32; 4],
    pub count: u64,
    pub bounds: [[f64; 3]; 2],
    pub spacing: f64,
    pub file: String,
    #[serde(default)] pub class_counts: Vec<(u8, u64)>,
}

#[derive(Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub source: String,
    pub stamp: String,
    pub source_points: u64,
    pub retained_points: u64,
    pub origin: [f64; 3],
    pub bounds_min: [f64; 3],
    pub bounds_max: [f64; 3],
    pub has_rgb: bool,
    pub has_intensity: bool,
    pub has_classification: bool,
    pub nodes: Vec<Node>,
}

struct NodeStats {
    key: [u32; 4],
    count: u64,
    bounds: [[f64; 3]; 2],
    classes: [u64; 256],
}

struct Writer {
    output: PathBuf,
    origin: [f64; 3],
    bounds: [[f64; 3]; 2],
    estimate: u64,
    seen: u64,
    kept: u64,
    nodes: HashMap<u64, NodeStats>,
    open: HashMap<u64, BufWriter<File>>,
    opened_order: VecDeque<u64>,
}

fn fingerprint(path: &Path) -> Result<String> {
    let info = fs::metadata(path)?;
    Ok(format!("{}:{}", info.len(), info.modified()?.duration_since(UNIX_EPOCH)?.as_nanos()))
}

fn hash(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e3779b97f4a7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d049bb133111eb);
    value ^ (value >> 31)
}

fn packed_key(key: [u32; 4]) -> u64 {
    (u64::from(key[0]) << 48) | (u64::from(key[1]) << 32)
        | (u64::from(key[2]) << 16) | u64::from(key[3])
}

impl Writer {
    fn new(output: &Path, preview: &Preview) -> Result<Self> {
        fs::create_dir_all(output.join("nodes"))?;
        let mut bounds = [preview.bounds_min, preview.bounds_max];
        for axis in 0..3 {
            let margin = ((bounds[1][axis] - bounds[0][axis]) * 0.01).max(0.1);
            bounds[0][axis] -= margin;
            bounds[1][axis] += margin;
        }
        Ok(Self { output: output.to_owned(), origin: preview.origin, bounds,
            estimate:preview.source_points.max(1), seen:0, kept:0,
            nodes:HashMap::new(), open:HashMap::new(), opened_order:VecDeque::new() })
    }

    fn level(&self, index: u64) -> u32 {
        let choice = hash(index) % self.estimate;
        let mut cumulative = 0u64;
        for level in 0..6 {
            cumulative = cumulative.saturating_add(16384u64.saturating_mul(8u64.pow(level)));
            if choice < cumulative { return level; }
        }
        6
    }

    fn key(&self, index: u64, xyz: [f64; 3]) -> [u32; 4] {
        let level = self.level(index);
        let cells = 1u32 << level;
        let mut key = [level, 0, 0, 0];
        for axis in 0..3 {
            let fraction = ((xyz[axis] - self.bounds[0][axis]) /
                (self.bounds[1][axis] - self.bounds[0][axis])).clamp(0.0, 1.0 - f64::EPSILON);
            key[axis + 1] = (fraction * f64::from(cells)) as u32;
        }
        key
    }

    fn node_writer(&mut self, packed: u64) -> Result<&mut BufWriter<File>> {
        if !self.open.contains_key(&packed) {
            if self.open.len() >= 128 {
                if let Some(oldest) = self.opened_order.pop_front() {
                    if let Some(mut writer) = self.open.remove(&oldest) { writer.flush()?; }
                }
            }
            let path = self.output.join("nodes").join(format!("{packed:016x}.bin"));
            let file = OpenOptions::new().create(true).append(true).open(path)?;
            self.open.insert(packed, BufWriter::new(file));
            self.opened_order.push_back(packed);
        }
        Ok(self.open.get_mut(&packed).unwrap())
    }

    fn finish(mut self, source: &Path, flags: [bool; 3]) -> Result<Manifest> {
        for (_, mut writer) in self.open.drain() { writer.flush()?; }
        let mut entries: Vec<_> = self.nodes.into_iter().collect();
        entries.sort_by_key(|(packed, _)| *packed);
        let mut nodes = Vec::with_capacity(entries.len());
        for (packed, stats) in entries {
            let file = format!("nodes/{packed:016x}.bin");
            let path = self.output.join(&file);
            shuffle_file(&path, packed, stats.count)?;
            let size = std::array::from_fn::<_, 3, _>(|i| stats.bounds[1][i] - stats.bounds[0][i]);
            let area = 2.0 * (size[0]*size[1] + size[0]*size[2] + size[1]*size[2]);
            let spacing = (area.max(1e-8) / stats.count as f64).sqrt();
            let class_counts = stats.classes.iter().enumerate().filter_map(|(class, count)|
                (*count > 0).then_some((class as u8, *count))).collect();
            nodes.push(Node { key:stats.key, count:stats.count, bounds:stats.bounds, spacing,
                file, class_counts });
        }
        let manifest = Manifest { version:1, source:source.to_string_lossy().into_owned(),
            stamp:fingerprint(source)?, source_points:self.seen,
            retained_points:self.kept, origin:self.origin,
            bounds_min:self.bounds[0], bounds_max:self.bounds[1],
            has_rgb:flags[0], has_intensity:flags[1], has_classification:flags[2], nodes };
        fs::write(self.output.join("manifest.json"), serde_json::to_vec_pretty(&manifest)?)?;
        Ok(manifest)
    }
}

impl PointSink for Writer {
    fn wants(&self) -> bool { true }
    fn skip(&mut self) { self.seen += 1; }
    fn push(&mut self, point: Point) -> Result<()> {
        let index = self.seen;
        self.seen += 1;
        if !point.xyz.iter().all(|value| value.is_finite()) { return Ok(()); }
        let key = self.key(index, point.xyz);
        let packed = packed_key(key);
        let stats = self.nodes.entry(packed).or_insert_with(|| NodeStats {
            key, count:0, bounds:[[f64::INFINITY;3],[f64::NEG_INFINITY;3]], classes:[0;256] });
        stats.count += 1;
        stats.classes[point.class as usize] += 1;
        for axis in 0..3 {
            stats.bounds[0][axis] = stats.bounds[0][axis].min(point.xyz[axis]);
            stats.bounds[1][axis] = stats.bounds[1][axis].max(point.xyz[axis]);
        }
        let mut record = [0u8; 18];
        for axis in 0..3 {
            record[axis*4..axis*4+4].copy_from_slice(&((point.xyz[axis]-self.origin[axis]) as f32).to_le_bytes());
        }
        record[12..15].copy_from_slice(&point.rgb);
        let intensity = (point.intensity.clamp(0.0, 1.0) * 65535.0).round() as u16;
        record[15..17].copy_from_slice(&intensity.to_le_bytes());
        record[17] = point.class;
        self.node_writer(packed)?.write_all(&record)?;
        self.kept += 1;
        Ok(())
    }
}

fn shuffle_file(path: &Path, seed: u64, count: u64) -> Result<()> {
    let mut data = fs::read(path)?;
    if data.len() != usize::try_from(count)? * 18 { bail!("Incomplete octree node: {}", path.display()); }
    let mut state = hash(seed);
    for index in (1..usize::try_from(count)?).rev() {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        let other = state as usize % (index + 1);
        if other == index { continue; }
        for byte in 0..18 { data.swap(index*18+byte, other*18+byte); }
    }
    fs::write(path, data)?;
    Ok(())
}

pub fn build(source: &Path, preview_cache: &Path, output: &Path) -> Result<()> {
    if output.exists() { bail!("Octree output already exists"); }
    let preview: Preview = serde_json::from_reader(BufReader::new(File::open(preview_cache.join("manifest.json"))?))?;
    let result = (|| -> Result<()> {
        let mut writer = Writer::new(output, &preview)?;
        let ext = source.extension().and_then(|value| value.to_str()).unwrap_or("").to_lowercase();
        let flags = match ext.as_str() {
            "ply" => super::parse_ply(source, &mut writer)?.1,
            "las" | "laz" => super::parse_las(source, &mut writer)?.1,
            "e57" => super::parse_e57(source, &mut writer)?.1,
            "zip" => {
                let mut archive = zip::ZipArchive::new(File::open(source)?)?;
                if archive.len() != 1 { bail!("ZIP must contain one scan file"); }
                let file = archive.by_index(0)?;
                super::parse_text(BufReader::new(file), &mut writer, true)?
            },
            "xyz" | "pts" | "asc" | "txt" | "csv" =>
                super::parse_text(BufReader::new(File::open(source)?), &mut writer, ext == "pts" || ext == "asc")?,
            _ => bail!("Unsupported octree input: {ext}"),
        };
        let manifest = writer.finish(source, flags)?;
        compact(output)?;
        println!("octree points={}", manifest.retained_points);
        Ok(())
    })();
    if result.is_err() { let _ = fs::remove_dir_all(output); }
    result.context("Octree build failed")
}

pub fn compact(root: &Path) -> Result<()> {
    let manifest_path = root.join("manifest.json");
    let mut manifest: Manifest = serde_json::from_reader(BufReader::new(File::open(&manifest_path)?))?;
    let mut groups: HashMap<[u32; 4], Vec<Node>> = HashMap::new();
    let mut nodes = Vec::new();
    for node in manifest.nodes.drain(..) {
        if node.count < 4096 && !node.file.starts_with("nodes/group-") {
            let key = [node.key[0], node.key[1] >> 2, node.key[2] >> 2, node.key[3] >> 2];
            groups.entry(key).or_default().push(node);
        } else { nodes.push(node); }
    }
    let temp = root.join("compact-tmp");
    if temp.exists() { bail!("A previous octree compaction is incomplete"); }
    fs::create_dir(&temp)?;
    let mut replaced = Vec::new();
    let mut added = Vec::new();
    for (key, members) in groups {
        if members.len() == 1 {
            nodes.push(members.into_iter().next().unwrap());
            continue;
        }
        let file = format!("nodes/group-{}-{}-{}-{}.bin", key[0], key[1], key[2], key[3]);
        let path = temp.join(Path::new(&file).file_name().unwrap());
        let mut writer = BufWriter::new(File::create(&path)?);
        let mut count = 0;
        let mut bounds = [[f64::INFINITY;3],[f64::NEG_INFINITY;3]];
        let mut class_counts = [0u64; 256];
        let node_key = members[0].key;
        for member in &members {
            count += member.count;
            for (class, count) in &member.class_counts { class_counts[*class as usize] += count; }
            for axis in 0..3 {
                bounds[0][axis] = bounds[0][axis].min(member.bounds[0][axis]);
                bounds[1][axis] = bounds[1][axis].max(member.bounds[1][axis]);
            }
            let mut reader = BufReader::new(File::open(root.join(&member.file))?);
            std::io::copy(&mut reader, &mut writer)?;
            replaced.push(member.file.clone());
        }
        writer.flush()?;
        shuffle_file(&path, packed_key(key), count)?;
        let size = std::array::from_fn::<_, 3, _>(|i| bounds[1][i]-bounds[0][i]);
        let area = 2.0 * (size[0]*size[1]+size[0]*size[2]+size[1]*size[2]);
        nodes.push(Node { key:node_key, count, bounds,
            spacing:(area.max(1e-8)/count as f64).sqrt(), file:file.clone(),
            class_counts:class_counts.iter().enumerate().filter_map(|(class, count)|
                (*count > 0).then_some((class as u8, *count))).collect() });
        added.push((path, root.join(&file)));
    }
    nodes.sort_by_key(|node| node.key);
    manifest.nodes = nodes;
    for (from, to) in &added { fs::rename(from, to)?; }
    let updated = root.join("manifest.json.tmp");
    fs::write(&updated, serde_json::to_vec_pretty(&manifest)?)?;
    fs::rename(updated, manifest_path)?;
    for file in replaced { fs::remove_file(root.join(file))?; }
    fs::remove_dir(&temp)?;
    println!("compacted nodes={} points={}", manifest.nodes.len(), manifest.retained_points);
    Ok(())
}

pub fn index_classes(root: &Path) -> Result<()> {
    let manifest_path = root.join("manifest.json");
    let mut manifest: Manifest = serde_json::from_reader(BufReader::new(File::open(&manifest_path)?))?;
    for node in &mut manifest.nodes {
        if !node.class_counts.is_empty() { continue; }
        let data = fs::read(root.join(&node.file))?;
        if data.len() != usize::try_from(node.count)? * 18 { bail!("Incomplete octree node"); }
        let mut counts = [0u64; 256];
        for record in data.chunks_exact(18) { counts[record[17] as usize] += 1; }
        node.class_counts = counts.iter().enumerate().filter_map(|(class, count)|
            (*count > 0).then_some((class as u8, *count))).collect();
    }
    let updated = root.join("manifest.json.tmp");
    fs::write(&updated, serde_json::to_vec_pretty(&manifest)?)?;
    fs::rename(updated, manifest_path)?;
    println!("indexed classes for {} nodes", manifest.nodes.len());
    Ok(())
}
