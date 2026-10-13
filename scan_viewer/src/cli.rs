//! Command dispatch for the Scan Viewer application.
//!
//! Processing modules remain private. This facade uses application errors so
//! the executable can report file and protocol failures with their context.

use crate::{
    cache::{CacheWriter, Manifest},
    formats::*,
    octree,
};
use anyhow::{bail, Context, Result};
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

fn import_preview(source: &Path, output: &Path, limit: u64) -> Result<Manifest> {
    if limit == 0 {
        bail!("--max-points must be positive");
    }
    if !source.is_file() {
        bail!("Source does not exist: {}", source.display());
    }
    let count = estimated_count(source)?;
    let mut out = CacheWriter::new(output, count, limit)?;
    let ext = source
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_lowercase();
    let (count, flags) = match ext.as_str() {
        "ply" => parse_ply(source, &mut out)?,
        "las" => parse_las(source, &mut out)?,
        "laz"
            if source
                .to_string_lossy()
                .to_lowercase()
                .ends_with(".copc.laz") =>
        {
            parse_copc(source, &mut out)?
        }
        "laz" => parse_las(source, &mut out)?,
        "e57" => parse_e57(source, &mut out)?,
        "zip" => {
            let mut z = zip::ZipArchive::new(File::open(source)?)?;
            let f = z.by_index(0)?;
            let flags = parse_text(BufReader::new(f), &mut out, true)?;
            (count, flags)
        }
        "xyz" | "pts" | "asc" | "txt" | "csv" => {
            let flags = parse_text(
                BufReader::new(File::open(source)?),
                &mut out,
                ext == "pts" || ext == "asc",
            )?;
            (count, flags)
        }
        _ => bail!("Unsupported scan format: {ext}"),
    };
    out.finish(source, count, flags)
}

/// Run a command with the executable name at `args[0]`.
///
/// Commands print JSON metadata or serve framed JSON and binary responses.
///
/// # Errors
/// Returns an error for invalid arguments, unsupported sources, malformed
/// cache data, or failed filesystem and protocol I/O.
pub fn run(args: &[String]) -> Result<()> {
    if args.len() == 2 && args[1] == "serve" {
        return crate::protocol::serve();
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
        return octree::build(
            Path::new(&args[2]),
            Path::new(&args[3]),
            Path::new(&args[4]),
        );
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
    let manifest = import_preview(Path::new(&args[1]), Path::new(&args[2]), limit)?;
    println!("{}", serde_json::to_string(&manifest)?);
    Ok(())
}
