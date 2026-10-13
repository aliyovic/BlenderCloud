use crate::point::{normalized_intensity, Point};
use anyhow::{anyhow, bail, Context, Result};
use las::copc::Entry;
use laz::record::{LayeredPointRecordDecompressor, RecordDecompressor};
use laz::DecompressionSelection;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;
use std::time::{Instant, UNIX_EPOCH};

pub(crate) struct Source {
    pub(super) reader: las::CopcReader<'static, BufReader<File>>,
    pub(super) node_file: BufReader<File>,
    pub(super) entries: Vec<Entry>,
    pub(super) bounds: Vec<[[f64; 3]; 2]>,
    pub(super) spacing: f64,
    pub(super) stamp: String,
    pub(super) class_counts: Option<Vec<Vec<(u8, u32)>>>,
    pub(super) last_used: Instant,
}

#[derive(Serialize, Deserialize)]
struct CopcClassIndex {
    pub(super) stamp: String,
    counts: Vec<Vec<(u8, u32)>>,
}

pub(crate) struct NativeSource {
    pub(super) manifest: crate::octree::Manifest,
    pub(super) root: String,
    pub(super) manifest_stamp: String,
    pub(super) last_used: Instant,
}

pub(super) fn native_source<'a>(
    native: &'a mut HashMap<String, NativeSource>,
    path: &str,
    cache: &str,
) -> Result<&'a NativeSource> {
    let source_stamp = stamp(path)?;
    let manifest_path = Path::new(cache).join("manifest.json");
    let manifest_stamp = stamp(manifest_path.to_str().context("Non-UTF8 cache path")?)?;
    let valid = native.get(cache).is_some_and(|cached| {
        cached.manifest.source == path
            && cached.manifest.stamp == source_stamp
            && cached.manifest_stamp == manifest_stamp
    });
    if !valid {
        native.remove(cache);
        let manifest: crate::octree::Manifest =
            serde_json::from_reader(File::open(manifest_path)?)?;
        if manifest.version != 1 || manifest.source != path || manifest.stamp != source_stamp {
            bail!("Octree cache is stale. Build it again for this source.");
        }
        if native.len() >= crate::protocol::MAX_OPEN_SOURCES {
            if let Some(oldest) = native
                .iter()
                .min_by_key(|(_, source)| source.last_used)
                .map(|(key, _)| key.clone())
            {
                native.remove(&oldest);
            }
        }
        native.insert(
            cache.to_owned(),
            NativeSource {
                manifest,
                root: cache.to_owned(),
                manifest_stamp,
                last_used: Instant::now(),
            },
        );
    }
    let cached = native
        .get_mut(cache)
        .context("Native source was not opened")?;
    cached.last_used = Instant::now();
    Ok(cached)
}

fn stamp(path: &str) -> Result<String> {
    let metadata = fs::metadata(path)?;
    Ok(format!(
        "{}:{}",
        metadata.len(),
        metadata.modified()?.duration_since(UNIX_EPOCH)?.as_nanos()
    ))
}

impl Source {
    fn open(path: &str, stamp: String) -> Result<Self> {
        let reader = las::CopcReader::from_path(path)?;
        let node_file = BufReader::new(File::open(path)?);
        let info = reader
            .header()
            .copc_info_vlr()
            .context("Missing COPC info")?;
        let file_bounds = reader.header().bounds();
        let mut entries: Vec<_> = reader
            .hierarchy_entries()
            .filter(|e| e.point_count > 0)
            .collect();
        entries.sort_by_key(|e| (e.key.l, e.key.x, e.key.y, e.key.z));
        let bounds = entries
            .iter()
            .map(|e| {
                let b = e.key.bounds(&info);
                [
                    [
                        b.min.x.max(file_bounds.min.x),
                        b.min.y.max(file_bounds.min.y),
                        b.min.z.max(file_bounds.min.z),
                    ],
                    [
                        b.max.x.min(file_bounds.max.x),
                        b.max.y.min(file_bounds.max.y),
                        b.max.z.min(file_bounds.max.z),
                    ],
                ]
            })
            .collect();
        Ok(Self {
            reader,
            node_file,
            entries,
            bounds,
            spacing: info.spacing,
            stamp,
            class_counts: None,
            last_used: Instant::now(),
        })
    }

    pub(super) fn ensure_class_counts(&mut self, cache: &str, budget: usize) -> Result<()> {
        if self.class_counts.is_some() {
            return Ok(());
        }
        let path = Path::new(cache).join("copc_class_counts.json");
        if let Ok(file) = File::open(&path) {
            if let Ok(index) = serde_json::from_reader::<_, CopcClassIndex>(BufReader::new(file)) {
                if index.stamp == self.stamp && index.counts.len() == self.entries.len() {
                    self.class_counts = Some(index.counts);
                    return Ok(());
                }
            }
        }
        let mut counts = Vec::with_capacity(self.entries.len());
        for entry in self.entries.clone() {
            let mut histogram = [0u32; 256];
            visit_copc_points(self, &entry, budget, |_, point| {
                histogram[usize::from(point.class)] += 1;
                Ok(())
            })?;
            counts.push(
                histogram
                    .iter()
                    .enumerate()
                    .filter_map(|(class, count)| (*count > 0).then_some((class as u8, *count)))
                    .collect(),
            );
        }
        if !cache.is_empty() {
            fs::create_dir_all(cache)?;
            let tmp = path.with_extension("json.tmp");
            fs::write(
                &tmp,
                serde_json::to_vec(&CopcClassIndex {
                    stamp: self.stamp.clone(),
                    counts: counts.clone(),
                })?,
            )?;
            fs::rename(tmp, path)?;
        }
        self.class_counts = Some(counts);
        Ok(())
    }
}

pub(super) fn source<'a>(
    sources: &'a mut HashMap<String, Source>,
    path: &str,
) -> Result<&'a mut Source> {
    let fingerprint = stamp(path)?;
    if sources.get(path).map(|source| source.stamp.as_str()) != Some(fingerprint.as_str()) {
        sources.remove(path);
        if sources.len() >= crate::protocol::MAX_OPEN_SOURCES {
            if let Some(oldest) = sources
                .iter()
                .min_by_key(|(_, source)| source.last_used)
                .map(|(key, _)| key.clone())
            {
                sources.remove(&oldest);
            }
        }
        sources.insert(path.to_owned(), Source::open(path, fingerprint)?);
    }
    let source = sources.get_mut(path).context("Source was not opened")?;
    source.last_used = Instant::now();
    Ok(source)
}

// LAS 1.4 decoders retain compressed layers, plus arithmetic models. They do not
// need an uncompressed whole-node buffer or a per-point permutation table.
pub(super) fn decoder_bytes(entry: &Entry) -> Result<usize> {
    usize::try_from(entry.byte_size)?
        .checked_mul(2)
        .and_then(|bytes| bytes.checked_add(4 * 1024 * 1024))
        .ok_or_else(|| anyhow!("COPC decoder size overflows"))
}

pub(super) fn visit_copc_points(
    src: &mut Source,
    entry: &Entry,
    budget: usize,
    mut visit: impl FnMut(usize, Point) -> Result<()>,
) -> Result<()> {
    let format = src.reader.header().point_format().to_u8()?;
    if (6..=8).contains(&format) {
        if decoder_bytes(entry)? > budget {
            bail!("COPC node exceeds decoder working budget. Repartition the source or raise working_budget_bytes.");
        }
        let transforms = *src.reader.header().transforms();
        let items = src.reader.header().laz_vlr()?.items().clone();
        src.node_file.seek(SeekFrom::Start(entry.offset))?;
        let mut decoder = LayeredPointRecordDecompressor::new(&mut src.node_file);
        decoder.set_fields_from(&items)?;
        let mut selection = DecompressionSelection::base()
            .decompress_z()
            .decompress_classification()
            .decompress_intensity();
        if format >= 7 {
            selection = selection.decompress_rgb();
        }
        decoder.set_selection(selection);
        let mut record = vec![0u8; decoder.record_size()];
        for index in 0..usize::try_from(entry.point_count)? {
            decoder.decompress_next(&mut record)?;
            let xyz = std::array::from_fn(|axis| {
                let raw = i32::from_le_bytes(
                    record[axis * 4..axis * 4 + 4]
                        .try_into()
                        .expect("LAS coordinate width"),
                );
                let transform = [&transforms.x, &transforms.y, &transforms.z][axis];
                f64::from(raw) * transform.scale + transform.offset
            });
            let rgb = if format >= 7 {
                [record[31], record[33], record[35]]
            } else {
                [220; 3]
            };
            let intensity = normalized_intensity(u16::from_le_bytes(record[12..14].try_into()?));
            visit(
                index,
                Point {
                    xyz,
                    rgb,
                    intensity,
                    class: record[16],
                },
            )?;
        }
    } else {
        let bytes = usize::try_from(entry.point_count)?
            .checked_mul(usize::from(src.reader.header().point_format().len()))
            .and_then(|size| size.checked_add(decoder_bytes(entry).ok()?))
            .ok_or_else(|| anyhow!("COPC node size overflows"))?;
        if bytes > budget {
            bail!("COPC node exceeds decoder working budget. Repartition the source or raise working_budget_bytes.");
        }
        let block = src.reader.read_entry(entry)?;
        for (index, point) in block.points().enumerate() {
            visit(index, Point::from_las(point?))?;
        }
    }
    Ok(())
}

pub(super) fn visit_native_points(
    root: &str,
    name: &str,
    count: usize,
    origin: [f64; 3],
    mut visit: impl FnMut(usize, Point) -> Result<()>,
) -> Result<()> {
    let path = crate::octree::node_path(Path::new(root), name)?;
    let file = File::open(&path)?;
    let expected = crate::protocol::record_bytes(count)?;
    if file.metadata()?.len() != u64::try_from(expected)? {
        bail!("Incomplete octree node count");
    }
    let mut reader = BufReader::new(file);
    let mut record = [0u8; 18];
    for index in 0..count {
        reader.read_exact(&mut record)?;
        let xyz = std::array::from_fn(|axis| {
            let raw = f32::from_le_bytes(
                record[axis * 4..axis * 4 + 4]
                    .try_into()
                    .expect("Native coordinate width"),
            );
            f64::from(raw) + origin[axis]
        });
        let point = Point {
            xyz,
            rgb: record[12..15].try_into()?,
            intensity: normalized_intensity(u16::from_le_bytes(record[15..17].try_into()?)),
            class: record[17],
        };
        visit(index, point)?;
    }
    if reader.read(&mut [0u8; 1])? != 0 {
        bail!("Octree node changed during read");
    }
    Ok(())
}

pub(super) fn write_packet(
    bytes: &mut [u8],
    count: usize,
    destination: usize,
    point: Point,
    origin: [f64; 3],
) {
    for (axis, coordinate) in point.xyz.into_iter().enumerate() {
        let local = (coordinate - origin[axis]) as f32;
        let offset = destination * 12 + axis * 4;
        bytes[offset..offset + 4].copy_from_slice(&local.to_le_bytes());
    }
    bytes[count * 12 + destination * 3..count * 12 + destination * 3 + 3]
        .copy_from_slice(&point.rgb);
    let intensity = (point.intensity * 65535.0).round() as u16;
    bytes[count * 15 + destination * 2..count * 15 + destination * 2 + 2]
        .copy_from_slice(&intensity.to_le_bytes());
    bytes[count * 17 + destination] = point.class;
}

pub(super) fn permutation_parameters(total: usize, seed: u64) -> (usize, usize) {
    if total <= 1 {
        return (0, 0);
    }
    fn gcd(mut a: usize, mut b: usize) -> usize {
        while b != 0 {
            (a, b) = (b, a % b);
        }
        a
    }
    let mut multiplier = (seed as usize % total).max(1);
    while gcd(multiplier, total) != 1 {
        multiplier = (multiplier + 1) % total;
    }
    (multiplier, seed.rotate_left(19) as usize % total)
}
