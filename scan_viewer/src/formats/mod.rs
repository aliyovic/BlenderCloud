use anyhow::{bail, Result};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

mod e57;
mod las;
mod ply;
mod text;

pub(crate) use e57::parse_e57;
pub(crate) use las::{copc_levels, parse_copc, parse_las};
pub(crate) use ply::parse_ply;
pub(crate) use text::parse_text;

fn color(v: f64) -> u8 {
    v.round().clamp(0.0, 255.0) as u8
}

fn normalized_color(v: f64) -> u8 {
    color(v * 255.0)
}

pub(crate) fn estimated_count(path: &Path) -> Result<u64> {
    let ext = path
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_lowercase();
    if ext == "las" || ext == "laz" {
        return Ok(::las::Reader::from_path(path)?.header().number_of_points());
    }
    if ext == "e57" {
        return Ok(::e57::E57Reader::from_file(path)?
            .pointclouds()
            .iter()
            .map(|x| x.records)
            .sum());
    }
    if ext == "zip" {
        let mut z = zip::ZipArchive::new(File::open(path)?)?;
        if z.len() != 1 {
            bail!("ZIP must contain one scan file");
        }
        let f = z.by_index(0)?;
        let size = f.size();
        let mut reader = BufReader::new(f);
        let mut line = String::new();
        for _ in 0..5 {
            line.clear();
            if reader.read_line(&mut line)? == 0 {
                break;
            }
            let s = line.trim();
            if s.starts_with('#') || s.starts_with("//") {
                continue;
            }
            if let Ok(count) = s.parse::<u64>() {
                return Ok(count);
            }
            break;
        }
        return Ok(size / 46);
    }
    if ext == "pts" || ext == "asc" {
        let mut reader = BufReader::new(File::open(path)?);
        let mut line = String::new();
        for _ in 0..5 {
            line.clear();
            if reader.read_line(&mut line)? == 0 {
                break;
            }
            let s = line.trim();
            if s.starts_with('#') || s.starts_with("//") {
                continue;
            }
            if let Ok(count) = s.parse::<u64>() {
                return Ok(count);
            }
            break;
        }
    }
    if ext == "ply" {
        for line in BufReader::new(File::open(path)?).lines().take(1000) {
            let s = line?;
            if let Some(count) = s.strip_prefix("element vertex ") {
                return Ok(count.trim().parse()?);
            }
            if s == "end_header" {
                break;
            }
        }
    }
    if matches!(ext.as_str(), "xyz" | "txt" | "csv") {
        let mut reader = BufReader::new(File::open(path)?);
        let mut row = Vec::new();
        let mut count = 0;
        let mut first = true;
        loop {
            row.clear();
            if reader.read_until(b'\n', &mut row)? == 0 {
                break;
            }
            let line = String::from_utf8_lossy(&row);
            let line = line.trim_start_matches('\u{feff}').trim();
            if line.is_empty() || line.starts_with('#') || line.starts_with("//") {
                continue;
            }
            if first {
                first = false;
                let column = line
                    .split(|c: char| c.is_whitespace() || c == ',' || c == ';')
                    .find(|part| !part.is_empty())
                    .unwrap_or("");
                if column.parse::<f64>().is_err() {
                    continue;
                }
            }
            count += 1;
        }
        return Ok(count);
    }
    Ok(0)
}
