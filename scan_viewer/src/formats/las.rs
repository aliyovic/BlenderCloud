use crate::cache::{CacheWriter, PointSink};
use crate::point::Point;
use anyhow::Result;
use std::collections::BTreeMap;
use std::path::Path;

pub(crate) fn parse_las<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = ::las::Reader::from_path(path)?;
    let count = r.header().number_of_points();
    let has_rgb = r.header().point_format().has_color;
    loop {
        let block = r.read_points(100_000)?;
        if block.is_empty() {
            break;
        }
        for item in block.points() {
            if !out.wants() {
                out.skip();
                continue;
            }
            let p = item?;
            out.push(Point::from_las(p))?;
        }
    }
    Ok((count, [has_rgb, true, true]))
}

pub(crate) fn copc_levels(path: &Path) -> Result<BTreeMap<i32, u64>> {
    let reader = ::las::CopcReader::from_path(path)?;
    let mut levels = BTreeMap::new();
    for entry in reader.hierarchy_entries() {
        if entry.point_count > 0 {
            *levels.entry(entry.key.l).or_insert(0) += entry.point_count as u64;
        }
    }
    Ok(levels)
}

pub(crate) fn parse_copc(path: &Path, out: &mut CacheWriter) -> Result<(u64, [bool; 3])> {
    let mut reader = ::las::CopcReader::from_path(path)?;
    let count = reader.header().number_of_points();
    let has_rgb = reader.header().point_format().has_color;
    let mut entries: Vec<_> = reader
        .hierarchy_entries()
        .filter(|entry| entry.point_count > 0)
        .collect();
    entries.sort_by_key(|entry| (entry.key.l, entry.key.x, entry.key.y, entry.key.z));
    let mut remaining = out.limit;
    let mut level_budget = BTreeMap::new();
    for entry in &entries {
        *level_budget.entry(entry.key.l).or_insert(0u64) += entry.point_count as u64;
    }
    let mut full_max = -1;
    for (&level, &points) in &level_budget {
        if points > remaining {
            break;
        }
        remaining -= points;
        full_max = level;
    }
    let next_level = full_max + 1;
    let next_count = *level_budget.get(&next_level).unwrap_or(&0);
    let fraction = if next_count > 0 {
        remaining as f64 / next_count as f64
    } else {
        0.0
    };
    out.stride = 1;
    for entry in entries {
        if entry.key.l > next_level || (entry.key.l == next_level && fraction <= 0.0) {
            continue;
        }
        let block = reader.read_entry(&entry)?;
        for (index, item) in block.points().enumerate() {
            if entry.key.l == next_level {
                let keep =
                    ((index + 1) as f64 * fraction).floor() > (index as f64 * fraction).floor();
                if !keep {
                    continue;
                }
            }
            let p = item?;
            out.push(Point::from_las(p))?;
        }
    }
    Ok((count, [has_rgb, true, true]))
}
