use super::normalized_color;
use crate::cache::PointSink;
use crate::point::Point;
use anyhow::Result;
use std::path::Path;

pub(crate) fn parse_e57<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = ::e57::E57Reader::from_file(path)?;
    let pcs = r.pointclouds();
    let count: u64 = pcs.iter().map(|pc| pc.records).sum();
    let flags = [
        pcs.iter().any(|pc| pc.has_color()),
        pcs.iter().any(|pc| pc.has_intensity()),
        false,
    ];
    for pc in pcs {
        for item in r.pointcloud_simple(&pc)? {
            if !out.wants() {
                item?;
                out.skip();
                continue;
            }
            let p = item?;
            let xyz = match p.cartesian {
                ::e57::CartesianCoordinate::Valid { x, y, z } => [x, y, z],
                _ => {
                    out.skip();
                    continue;
                }
            };
            let rgb = p
                .color
                .map(|c| {
                    [
                        normalized_color(c.red as f64),
                        normalized_color(c.green as f64),
                        normalized_color(c.blue as f64),
                    ]
                })
                .unwrap_or([220; 3]);
            out.push(Point {
                xyz,
                rgb,
                intensity: p.intensity.unwrap_or(0.0),
                class: 0,
            })?;
        }
    }
    Ok((count, flags))
}
