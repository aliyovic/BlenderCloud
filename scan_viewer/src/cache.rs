use crate::point::Point;
use anyhow::{bail, Result};
use serde::Serialize;
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

#[derive(Serialize)]
pub(crate) struct Manifest {
    version: u32,
    source: String,
    source_points: u64,
    retained_points: u64,
    pub(crate) stride: u64,
    pub(crate) origin: [f64; 3],
    bounds_min: [f64; 3],
    bounds_max: [f64; 3],
    columns: [&'static str; 4],
    has_rgb: bool,
    has_intensity: bool,
    has_classification: bool,
}

pub(crate) struct CacheWriter {
    output: PathBuf,
    positions: BufWriter<File>,
    colors: BufWriter<File>,
    intensities: BufWriter<File>,
    classes: BufWriter<File>,
    pub(crate) origin: Option<[f64; 3]>,
    min: [f64; 3],
    max: [f64; 3],
    pub(crate) retained: u64,
    seen: u64,
    pub(crate) stride: u64,
    pub(crate) limit: u64,
}

impl CacheWriter {
    pub(crate) fn new(output: &Path, count: u64, limit: u64) -> Result<Self> {
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
            stride: count
                .saturating_add(limit - 1)
                .checked_div(limit)
                .unwrap_or(1)
                .max(1),
            limit,
        })
    }

    fn wants(&self) -> bool {
        self.seen.is_multiple_of(self.stride) && self.retained < self.limit
    }

    fn skip(&mut self) {
        self.seen += 1;
    }

    pub(crate) fn push(&mut self, p: Point) -> Result<bool> {
        self.seen += 1;
        if !p.xyz.iter().all(|x| x.is_finite()) {
            return Ok(false);
        }
        let origin = *self.origin.get_or_insert_with(|| {
            if p.xyz.iter().any(|x| x.abs() > 100_000.0) {
                p.xyz.map(|x| (x / 1000.0).floor() * 1000.0)
            } else {
                [0.0; 3]
            }
        });
        for (i, coordinate) in p.xyz.into_iter().enumerate() {
            self.min[i] = self.min[i].min(coordinate);
            self.max[i] = self.max[i].max(coordinate);
            self.positions
                .write_all(&((coordinate - origin[i]) as f32).to_le_bytes())?;
        }
        self.colors.write_all(&p.rgb)?;
        self.intensities.write_all(&p.intensity.to_le_bytes())?;
        self.classes.write_all(&[p.class])?;
        self.retained += 1;
        Ok(true)
    }

    pub(crate) fn finish(
        mut self,
        source: &Path,
        count: u64,
        flags: [bool; 3],
    ) -> Result<Manifest> {
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
            columns: [
                "positions.f32",
                "colors.u8",
                "intensity.f32",
                "classification.u8",
            ],
            has_rgb: flags[0],
            has_intensity: flags[1],
            has_classification: flags[2],
        };
        fs::write(
            self.output.join("manifest.json"),
            serde_json::to_vec_pretty(&manifest)?,
        )?;
        Ok(manifest)
    }
}

pub(crate) trait PointSink {
    fn wants(&self) -> bool;
    fn skip(&mut self);
    fn push(&mut self, point: Point) -> Result<()>;
}

impl PointSink for CacheWriter {
    fn wants(&self) -> bool {
        CacheWriter::wants(self)
    }
    fn skip(&mut self) {
        CacheWriter::skip(self)
    }
    fn push(&mut self, point: Point) -> Result<()> {
        CacheWriter::push(self, point).map(|_| ())
    }
}
