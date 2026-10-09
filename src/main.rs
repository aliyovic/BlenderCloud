use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

mod streaming;
mod octree;

#[derive(Clone, Copy)]
struct Point {
    xyz: [f64; 3],
    rgb: [u8; 3],
    intensity: f32,
    class: u8,
}

#[derive(Serialize)]
struct Manifest {
    version: u32,
    source: String,
    source_points: u64,
    retained_points: u64,
    stride: u64,
    origin: [f64; 3],
    bounds_min: [f64; 3],
    bounds_max: [f64; 3],
    columns: [&'static str; 4],
    has_rgb: bool,
    has_intensity: bool,
    has_classification: bool,
}

struct CacheWriter {
    output: PathBuf,
    positions: BufWriter<File>,
    colors: BufWriter<File>,
    intensities: BufWriter<File>,
    classes: BufWriter<File>,
    origin: Option<[f64; 3]>,
    min: [f64; 3],
    max: [f64; 3],
    retained: u64,
    seen: u64,
    stride: u64,
    limit: u64,
}

impl CacheWriter {
    fn new(output: &Path, count: u64, limit: u64) -> Result<Self> {
        fs::create_dir_all(output)?;
        let file = |name| File::create(output.join(name));
        Ok(Self {
            output: output.to_owned(),
            positions: BufWriter::new(file("positions.f32")?),
            colors: BufWriter::new(file("colors.u8")?),
            intensities: BufWriter::new(file("intensity.f32")?),
            classes: BufWriter::new(file("classification.u8")?),
            origin: None,
            min: [f64::INFINITY; 3],
            max: [f64::NEG_INFINITY; 3],
            retained: 0,
            seen: 0,
            stride: count.saturating_add(limit - 1).checked_div(limit).unwrap_or(1).max(1),
            limit,
        })
    }

    fn wants(&self) -> bool {
        self.seen % self.stride == 0 && self.retained < self.limit
    }

    fn skip(&mut self) {
        self.seen += 1;
    }

    fn push(&mut self, p: Point) -> Result<()> {
        self.seen += 1;
        if !p.xyz.iter().all(|x| x.is_finite()) {
            return Ok(());
        }
        let origin = *self.origin.get_or_insert_with(|| {
            if p.xyz.iter().any(|x| x.abs() > 100_000.0) {
                p.xyz.map(|x| (x / 1000.0).floor() * 1000.0)
            } else {
                [0.0; 3]
            }
        });
        for i in 0..3 {
            self.min[i] = self.min[i].min(p.xyz[i]);
            self.max[i] = self.max[i].max(p.xyz[i]);
            self.positions.write_all(&((p.xyz[i] - origin[i]) as f32).to_le_bytes())?;
        }
        self.colors.write_all(&p.rgb)?;
        self.intensities.write_all(&p.intensity.to_le_bytes())?;
        self.classes.write_all(&[p.class])?;
        self.retained += 1;
        Ok(())
    }

    fn finish(mut self, source: &Path, count: u64, flags: [bool; 3]) -> Result<Manifest> {
        self.positions.flush()?;
        self.colors.flush()?;
        self.intensities.flush()?;
        self.classes.flush()?;
        if self.retained == 0 {
            bail!("No valid points found in {}", source.display());
        }
        let manifest = Manifest {
            version: 1,
            source: source.to_string_lossy().into_owned(),
            source_points: count.max(self.seen),
            retained_points: self.retained,
            stride: self.stride,
            origin: self.origin.unwrap(),
            bounds_min: self.min,
            bounds_max: self.max,
            columns: ["positions.f32", "colors.u8", "intensity.f32", "classification.u8"],
            has_rgb: flags[0],
            has_intensity: flags[1],
            has_classification: flags[2],
        };
        fs::write(self.output.join("manifest.json"), serde_json::to_vec_pretty(&manifest)?)?;
        Ok(manifest)
    }
}

trait PointSink {
    fn wants(&self) -> bool;
    fn skip(&mut self);
    fn push(&mut self, point: Point) -> Result<()>;
}

impl PointSink for CacheWriter {
    fn wants(&self) -> bool { CacheWriter::wants(self) }
    fn skip(&mut self) { CacheWriter::skip(self) }
    fn push(&mut self, point: Point) -> Result<()> { CacheWriter::push(self, point) }
}

fn color(v: f64) -> u8 {
    v.round().clamp(0.0, 255.0) as u8
}

fn normalized_color(v: f64) -> u8 {
    color(v * 255.0)
}

fn ply_width(kind: &str) -> Result<usize> {
    match kind {
        "char" | "int8" | "uchar" | "uint8" => Ok(1),
        "short" | "int16" | "ushort" | "uint16" => Ok(2),
        "int" | "int32" | "uint" | "uint32" | "float" | "float32" => Ok(4),
        "double" | "float64" => Ok(8),
        _ => bail!("Unsupported PLY property type: {kind}"),
    }
}

fn ply_number(bytes: &[u8], kind: &str, big: bool) -> f64 {
    match kind {
        "char" | "int8" => (bytes[0] as i8) as f64,
        "uchar" | "uint8" => bytes[0] as f64,
        "short" | "int16" => (if big { i16::from_be_bytes([bytes[0], bytes[1]]) } else { i16::from_le_bytes([bytes[0], bytes[1]]) }) as f64,
        "ushort" | "uint16" => (if big { u16::from_be_bytes([bytes[0], bytes[1]]) } else { u16::from_le_bytes([bytes[0], bytes[1]]) }) as f64,
        "int" | "int32" => (if big { i32::from_be_bytes(bytes.try_into().unwrap()) } else { i32::from_le_bytes(bytes.try_into().unwrap()) }) as f64,
        "uint" | "uint32" => (if big { u32::from_be_bytes(bytes.try_into().unwrap()) } else { u32::from_le_bytes(bytes.try_into().unwrap()) }) as f64,
        "float" | "float32" => (if big { f32::from_be_bytes(bytes.try_into().unwrap()) } else { f32::from_le_bytes(bytes.try_into().unwrap()) }) as f64,
        "double" | "float64" => if big { f64::from_be_bytes(bytes.try_into().unwrap()) } else { f64::from_le_bytes(bytes.try_into().unwrap()) },
        _ => unreachable!(),
    }
}

fn parse_text<R: BufRead, S: PointSink>(mut reader: R, out: &mut S, asc_header: bool) -> Result<[bool; 3]> {
    let mut line = String::new();
    let mut first = true;
    let mut flags = [false; 3];
    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 { break; }
        let s = line.trim();
        if s.is_empty() || s.starts_with('#') || s.starts_with("//") { continue; }
        if first {
            first = false;
            if asc_header && s.parse::<u64>().is_ok() { continue; }
            if s.split(|c: char| c.is_whitespace() || c == ',' || c == ';')
                .find(|x| !x.is_empty()).unwrap_or("").parse::<f64>().is_err() { continue; }
        }
        if !out.wants() { out.skip(); continue; }
        let a: Vec<&str> = s.split(|c: char| c.is_whitespace() || c == ',' || c == ';')
            .filter(|x| !x.is_empty()).collect();
        if a.len() < 3 { continue; }
        let xyz = [a[0].parse()?, a[1].parse()?, a[2].parse()?];
        let rgb = if a.len() >= 6 {
            flags[0] = true;
            [color(a[3].parse()?), color(a[4].parse()?), color(a[5].parse()?)]
        } else { [220, 220, 220] };
        let intensity = if a.len() > 6 { flags[1] = true; a[6].parse()? } else { 0.0 };
        let class = if a.len() > 7 { flags[2] = true; a[7].parse::<f64>()? as u8 } else { 0 };
        out.push(Point { xyz, rgb, intensity, class })?;
    }
    Ok(flags)
}

fn parse_ply<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = BufReader::new(File::open(path)?);
    let mut line = String::new();
    r.read_line(&mut line)?;
    if line.trim() != "ply" { bail!("Not a PLY file"); }
    let mut format = String::new();
    let mut count = 0;
    let mut in_vertex = false;
    let mut props: Vec<(String, String)> = Vec::new();
    loop {
        line.clear();
        if r.read_line(&mut line)? == 0 { bail!("PLY header has no end_header"); }
        let a: Vec<_> = line.split_whitespace().collect();
        if a.is_empty() { continue; }
        match a[0] {
            "format" => format = a.get(1).unwrap_or(&"").to_string(),
            "element" => { in_vertex = a.get(1) == Some(&"vertex"); if in_vertex { count = a.get(2).ok_or_else(|| anyhow!("Missing vertex count"))?.parse()?; } },
            "property" if in_vertex => {
                if a.len() != 3 { bail!("PLY list vertex properties are unsupported"); }
                props.push((a[1].to_string(), a[2].to_string()));
            },
            "end_header" => break,
            _ => (),
        }
    }
    let index = |name: &str| props.iter().position(|p| p.1 == name);
    let x = index("x").ok_or_else(|| anyhow!("PLY has no x"))?;
    let y = index("y").ok_or_else(|| anyhow!("PLY has no y"))?;
    let z = index("z").ok_or_else(|| anyhow!("PLY has no z"))?;
    let rgb_idx = [index("red"), index("green"), index("blue")];
    let intensity_idx = index("intensity");
    let class_idx = index("classification");
    let flags = [rgb_idx.iter().all(Option::is_some), intensity_idx.is_some(), class_idx.is_some()];
    if format == "ascii" {
        for _ in 0..count {
            line.clear();
            if r.read_line(&mut line)? == 0 { bail!("PLY ended before vertex count"); }
            if !out.wants() { out.skip(); continue; }
            let a: Vec<_> = line.split_whitespace().collect();
            let number = |i: usize| -> Result<f64> { Ok(a.get(i).ok_or_else(|| anyhow!("Short PLY row"))?.parse()?) };
            let xyz = [number(x)?, number(y)?, number(z)?];
            let rgb = if flags[0] { rgb_idx.map(|i| color(number(i.unwrap()).unwrap_or(0.0))) } else { [220; 3] };
            let intensity = if let Some(i) = intensity_idx { number(i)? as f32 } else { 0.0 };
            let class = if let Some(i) = class_idx { number(i)? as u8 } else { 0 };
            out.push(Point { xyz, rgb, intensity, class })?;
        }
    } else if format == "binary_little_endian" || format == "binary_big_endian" {
        let big = format == "binary_big_endian";
        let mut offsets = Vec::new();
        let mut row_width = 0;
        for (kind, _) in &props {
            offsets.push(row_width);
            row_width += ply_width(kind)?;
        }
        let mut row = vec![0u8; row_width];
        for _ in 0..count {
            r.read_exact(&mut row)?;
            if !out.wants() { out.skip(); continue; }
            let number = |i: usize| -> f64 {
                let kind = props[i].0.as_str();
                let start = offsets[i];
                ply_number(&row[start..start + ply_width(kind).unwrap()], kind, big)
            };
            let xyz = [number(x), number(y), number(z)];
            let rgb = if flags[0] {
                rgb_idx.map(|i| {
                    let j = i.unwrap();
                    if ply_width(&props[j].0).unwrap() == 2 { color(number(j) / 257.0) } else { color(number(j)) }
                })
            } else { [220; 3] };
            let intensity = intensity_idx.map(|i| number(i) as f32).unwrap_or(0.0);
            let class = class_idx.map(|i| number(i) as u8).unwrap_or(0);
            out.push(Point { xyz, rgb, intensity, class })?;
        }
    } else {
        bail!("Unsupported PLY format: {format}");
    }
    Ok((count, flags))
}

fn parse_las<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = las::Reader::from_path(path)?;
    let count = r.header().number_of_points();
    let has_rgb = r.header().point_format().has_color;
    loop {
        let block = r.read_points(100_000)?;
        if block.is_empty() { break; }
        for item in block.points() {
            if !out.wants() { out.skip(); continue; }
            let p = item?;
            let rgb = p.color.map(|c| [(c.red >> 8) as u8, (c.green >> 8) as u8, (c.blue >> 8) as u8]).unwrap_or([220; 3]);
            out.push(Point {
                xyz: [p.x, p.y, p.z], rgb,
                intensity: p.intensity as f32 / 65535.0,
                class: u8::from(p.classification),
            })?;
        }
    }
    Ok((count, [has_rgb, true, true]))
}

fn copc_levels(path: &Path) -> Result<BTreeMap<i32, u64>> {
    let reader = las::CopcReader::from_path(path)?;
    let mut levels = BTreeMap::new();
    for entry in reader.hierarchy_entries() {
        if entry.point_count > 0 {
            *levels.entry(entry.key.l).or_insert(0) += entry.point_count as u64;
        }
    }
    Ok(levels)
}

fn parse_copc(path: &Path, out: &mut CacheWriter) -> Result<(u64, [bool; 3])> {
    let mut reader = las::CopcReader::from_path(path)?;
    let count = reader.header().number_of_points();
    let has_rgb = reader.header().point_format().has_color;
    let mut entries: Vec<_> = reader.hierarchy_entries()
        .filter(|entry| entry.point_count > 0).collect();
    entries.sort_by_key(|entry| (entry.key.l, entry.key.x, entry.key.y, entry.key.z));
    let mut remaining = out.limit;
    let mut level_budget = BTreeMap::new();
    for entry in &entries {
        *level_budget.entry(entry.key.l).or_insert(0u64) += entry.point_count as u64;
    }
    let mut full_max = -1;
    for (&level, &points) in &level_budget {
        if points > remaining { break; }
        remaining -= points;
        full_max = level;
    }
    let next_level = full_max + 1;
    let next_count = *level_budget.get(&next_level).unwrap_or(&0);
    let fraction = if next_count > 0 { remaining as f64 / next_count as f64 } else { 0.0 };
    out.stride = 1;
    for entry in entries {
        if entry.key.l > next_level || (entry.key.l == next_level && fraction <= 0.0) {
            continue;
        }
        let block = reader.read_entry(&entry)?;
        for (index, item) in block.points().enumerate() {
            if entry.key.l == next_level {
                let keep = ((index + 1) as f64 * fraction).floor() > (index as f64 * fraction).floor();
                if !keep { continue; }
            }
            let p = item?;
            let rgb = p.color.map(|c| [(c.red >> 8) as u8, (c.green >> 8) as u8, (c.blue >> 8) as u8]).unwrap_or([220; 3]);
            out.push(Point {
                xyz: [p.x, p.y, p.z], rgb,
                intensity: p.intensity as f32,
                class: u8::from(p.classification),
            })?;
        }
    }
    Ok((count, [has_rgb, true, true]))
}

fn parse_e57<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = e57::E57Reader::from_file(path)?;
    let pcs = r.pointclouds();
    let count: u64 = pcs.iter().map(|pc| pc.records).sum();
    let flags = [pcs.iter().any(|pc| pc.has_color()), pcs.iter().any(|pc| pc.has_intensity()), false];
    for pc in pcs {
        for item in r.pointcloud_simple(&pc)? {
            if !out.wants() { item?; out.skip(); continue; }
            let p = item?;
            let xyz = match p.cartesian {
                e57::CartesianCoordinate::Valid { x, y, z } => [x, y, z],
                _ => { out.skip(); continue; }
            };
            let rgb = p.color.map(|c| [normalized_color(c.red as f64), normalized_color(c.green as f64), normalized_color(c.blue as f64)]).unwrap_or([220; 3]);
            out.push(Point { xyz, rgb, intensity: p.intensity.unwrap_or(0.0), class: 0 })?;
        }
    }
    Ok((count, flags))
}

fn estimated_count(path: &Path) -> Result<u64> {
    let ext = path.extension().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
    if ext == "las" || ext == "laz" { return Ok(las::Reader::from_path(path)?.header().number_of_points()); }
    if ext == "e57" { return Ok(e57::E57Reader::from_file(path)?.pointclouds().iter().map(|x| x.records).sum()); }
    if ext == "zip" {
        let mut z = zip::ZipArchive::new(File::open(path)?)?;
        if z.len() != 1 { bail!("ZIP must contain one scan file"); }
        let f = z.by_index(0)?;
        let size = f.size();
        let mut reader = BufReader::new(f);
        let mut line = String::new();
        for _ in 0..5 {
            line.clear();
            if reader.read_line(&mut line)? == 0 { break; }
            let s = line.trim();
            if s.starts_with('#') || s.starts_with("//") { continue; }
            if let Ok(count) = s.parse::<u64>() { return Ok(count); }
            break;
        }
        return Ok(size / 46);
    }
    if ext == "pts" || ext == "asc" {
        let mut reader = BufReader::new(File::open(path)?);
        let mut line = String::new();
        for _ in 0..5 {
            line.clear();
            if reader.read_line(&mut line)? == 0 { break; }
            let s = line.trim();
            if s.starts_with('#') || s.starts_with("//") { continue; }
            if let Ok(count) = s.parse::<u64>() { return Ok(count); }
            break;
        }
    }
    if ext == "ply" {
        for line in BufReader::new(File::open(path)?).lines().take(1000) {
            let s = line?;
            if s.starts_with("element vertex ") { return Ok(s[15..].trim().parse()?); }
            if s == "end_header" { break; }
        }
    }
    if matches!(ext.as_str(), "xyz" | "txt" | "csv") {
        let mut reader = BufReader::new(File::open(path)?);
        let mut row = Vec::new();
        let mut count = 0;
        let mut first = true;
        loop {
            row.clear();
            if reader.read_until(b'\n', &mut row)? == 0 { break; }
            let line = String::from_utf8_lossy(&row);
            let line = line.trim_start_matches('\u{feff}').trim();
            if line.is_empty() || line.starts_with('#') || line.starts_with("//") { continue; }
            if first {
                first = false;
                let column = line.split(|c: char| c.is_whitespace() || c == ',' || c == ';')
                    .find(|part| !part.is_empty()).unwrap_or("");
                if column.parse::<f64>().is_err() { continue; }
            }
            count += 1;
        }
        return Ok(count);
    }
    Ok(0)
}

fn run(source: &Path, output: &Path, limit: u64) -> Result<Manifest> {
    if limit == 0 { bail!("--max-points must be positive"); }
    if !source.is_file() { bail!("Source does not exist: {}", source.display()); }
    let count = estimated_count(source)?;
    let mut out = CacheWriter::new(output, count, limit)?;
    let ext = source.extension().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
    let (count, flags) = match ext.as_str() {
        "ply" => parse_ply(source, &mut out)?,
        "las" => parse_las(source, &mut out)?,
        "laz" if source.to_string_lossy().to_lowercase().ends_with(".copc.laz") => parse_copc(source, &mut out)?,
        "laz" => parse_las(source, &mut out)?,
        "e57" => parse_e57(source, &mut out)?,
        "zip" => {
            let mut z = zip::ZipArchive::new(File::open(source)?)?;
            let f = z.by_index(0)?;
            let flags = parse_text(BufReader::new(f), &mut out, true)?;
            (count, flags)
        },
        "xyz" | "pts" | "asc" | "txt" | "csv" => {
            let flags = parse_text(BufReader::new(File::open(source)?), &mut out, ext == "pts" || ext == "asc")?;
            (count, flags)
        },
        _ => bail!("Unsupported scan format: {ext}"),
    };
    out.finish(source, count, flags)
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 2 && args[1] == "serve" {
        return streaming::serve();
    }
    if args.len() == 3 && args[1] == "inspect-origin" {
        let reader = las::Reader::from_path(&args[2])?;
        let min = reader.header().bounds().min;
        let origin = [min.x, min.y, min.z].map(|value| (value / 1000.0).floor() * 1000.0);
        println!("{}", serde_json::to_string(&origin)?);
        return Ok(());
    }
    if args.len() == 3 && args[1] == "inspect-copc" {
        let levels = copc_levels(Path::new(&args[2]))?;
        println!("{}", serde_json::to_string(&levels)?);
        return Ok(());
    }
    if args.len() == 5 && args[1] == "build-octree" {
        return octree::build(Path::new(&args[2]), Path::new(&args[3]), Path::new(&args[4]));
    }
    if args.len() == 3 && args[1] == "compact-octree" {
        return octree::compact(Path::new(&args[2]));
    }
    if args.len() == 3 && args[1] == "index-octree-classes" {
        return octree::index_classes(Path::new(&args[2]));
    }
    if args.len() != 5 || args[3] != "--max-points" {
        bail!("Usage: scan-cache SOURCE CACHE_DIRECTORY --max-points N");
    }
    let limit = args[4].parse().context("Invalid max point count")?;
    let manifest = run(Path::new(&args[1]), Path::new(&args[2]), limit)?;
    println!("{}", serde_json::to_string(&manifest)?);
    Ok(())
}
