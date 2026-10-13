use super::source::{
    native_source, source, visit_copc_points, visit_native_points, NativeSource, Source,
};
use super::transform;
use crate::point::Point;
use crate::protocol::{CropRequest, CropSource, Matrix};
use anyhow::{bail, Result};
use las::copc::Entry;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::Path;

pub(super) fn intersects(a: [[f64; 3]; 2], b: [[f64; 3]; 2]) -> bool {
    (0..3).all(|i| a[0][i] <= b[1][i] && a[1][i] >= b[0][i])
}

fn crop_point(
    req: &CropRequest,
    spec: &CropSource,
    xyz: [f64; 3],
    rgb: [u8; 3],
    intensity: f32,
    class: u8,
) -> Result<Option<Point>> {
    if !(0..3).all(|i| xyz[i] >= spec.bounds[0][i] && xyz[i] <= spec.bounds[1][i]) {
        return Ok(None);
    }
    let local = std::array::from_fn(|i| xyz[i] - spec.origin[i]);
    if let Some(matrix) = &spec.clip_matrix {
        let q = transform(matrix, local);
        if q[..3].iter().any(|v| v.abs() > 1.0 + 1e-9) {
            return Ok(None);
        }
    }
    let output_xyz = if let Some(matrix) = &spec.to_output {
        let q = transform(matrix, local);
        std::array::from_fn(|i| req.origin[i] + q[i])
    } else {
        xyz
    };
    if !output_xyz.iter().all(|coordinate| coordinate.is_finite())
        || !(0..3).all(|axis| ((output_xyz[axis] - req.origin[axis]) as f32).is_finite())
    {
        bail!("Crop transform produces coordinates outside finite local storage");
    }
    Ok(Some(Point {
        xyz: output_xyz,
        rgb,
        intensity,
        class,
    }))
}

fn validate_crop_transform(matrix: &Matrix) -> Result<()> {
    if !matrix.iter().flatten().all(|value| value.is_finite()) || matrix[3] != [0.0, 0.0, 0.0, 1.0]
    {
        bail!("Crop transform must be finite and affine with homogeneous w=1");
    }
    Ok(())
}

fn write_crop_point(
    req: &CropRequest,
    source_index: usize,
    node_id: usize,
    point_index: usize,
    point: Point,
    out: &mut crate::cache::CacheWriter,
    ids: &mut BufWriter<File>,
) -> Result<()> {
    if source_index > 255 || node_id > 0x00ff_ffff || point_index > u32::MAX as usize {
        bail!("Crop source ID exceeds protocol limits");
    }
    if out.retained >= req.point_limit {
        bail!(
            "Crop exceeds {} points. Shrink the modeling box or raise the crop limit.",
            req.point_limit
        );
    }
    if !out.push(point)? {
        bail!("Crop transform produced an invalid point");
    }
    let node_id = ((source_index as u64) << 24) | node_id as u64;
    ids.write_all(&((node_id << 32) | point_index as u64).to_le_bytes())?;
    Ok(())
}

pub(crate) fn crop(
    req: &CropRequest,
    sources: &mut HashMap<String, Source>,
    native: &mut HashMap<String, NativeSource>,
) -> Result<Value> {
    if req.point_limit == 0 || req.point_limit > 5_000_000 {
        bail!("Crop point limit must be 1 to 5,000,000");
    }
    let source_specs = if req.source_specs.is_empty() {
        vec![CropSource {
            source: req.source.clone(),
            origin: req.origin,
            bounds: req.bounds,
            cache: String::new(),
            clip_matrix: req.clip_matrix,
            to_output: None,
            stamp: req.stamp.clone(),
        }]
    } else {
        req.source_specs.clone()
    };
    if source_specs.len() > 256 {
        bail!("A crop can use at most 256 sources");
    }
    if !req.origin.iter().all(|value| value.is_finite()) {
        bail!("Crop transform origin must be finite");
    }
    for spec in &source_specs {
        if !(0..3).all(|i| {
            spec.bounds[0][i].is_finite()
                && spec.bounds[1][i].is_finite()
                && spec.bounds[0][i] < spec.bounds[1][i]
        }) {
            bail!("Invalid crop bounds");
        }
        if !spec.origin.iter().all(|value| value.is_finite()) {
            bail!("Crop transform origin must be finite");
        }
        for matrix in [&spec.to_output, &spec.clip_matrix].into_iter().flatten() {
            validate_crop_transform(matrix)?;
        }
    }
    let output = Path::new(&req.output);
    if output.exists() {
        bail!("Crop output already exists");
    }
    let result = (|| -> Result<Value> {
        let mut out = crate::cache::CacheWriter::new(output, 0, req.point_limit)?;
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
                if !spec.stamp.is_empty() && spec.stamp != src.stamp {
                    bail!("Source changed before crop");
                }
                source_points += src.reader.header().number_of_points();
                has_rgb |= src.reader.header().point_format().has_color;
                source_stamps.push(json!({"source":spec.source,"stamp":src.stamp}));
                let work: Vec<(usize, Entry)> = src
                    .entries
                    .iter()
                    .copied()
                    .enumerate()
                    .filter(|(id, _)| intersects(src.bounds[*id], spec.bounds))
                    .collect();
                if work.iter().any(|(id, _)| *id > 0x00ff_ffff) {
                    bail!("Too many nodes in crop source");
                }
                for (id, entry) in work {
                    let mut included = false;
                    visit_copc_points(src, &entry, req.working_budget_bytes, |index, point| {
                        if let Some(point) = crop_point(
                            req,
                            spec,
                            point.xyz,
                            point.rgb,
                            point.intensity,
                            point.class,
                        )? {
                            write_crop_point(
                                req,
                                source_index,
                                id,
                                index,
                                point,
                                &mut out,
                                &mut ids,
                            )?;
                            included = true;
                        }
                        Ok(())
                    })?;
                    decoded += u64::try_from(entry.point_count)?;
                    if included {
                        node_keys.push(json!({"source_index":source_index,"id":id,
                        "key":[entry.key.l,entry.key.x,entry.key.y,entry.key.z]}));
                    }
                }
            } else {
                let src = native_source(native, &spec.source, &spec.cache)?;
                if !spec.stamp.is_empty() && spec.stamp != src.manifest.stamp {
                    bail!("Source changed before crop");
                }
                source_points += src.manifest.source_points;
                has_rgb |= src.manifest.has_rgb;
                source_stamps.push(
                    json!({"source":spec.source,"stamp":src.manifest.stamp,"cache":spec.cache}),
                );
                for (id, entry) in src.manifest.nodes.iter().enumerate() {
                    if !intersects(entry.bounds, spec.bounds) {
                        continue;
                    }
                    if id > 0x00ff_ffff {
                        bail!("Too many nodes in crop source");
                    }
                    decoded += entry.count;
                    let mut included = false;
                    visit_native_points(
                        &src.root,
                        &entry.file,
                        usize::try_from(entry.count)?,
                        src.manifest.origin,
                        |index, point| {
                            if let Some(point) = crop_point(
                                req,
                                spec,
                                point.xyz,
                                point.rgb,
                                point.intensity,
                                point.class,
                            )? {
                                write_crop_point(
                                    req,
                                    source_index,
                                    id,
                                    index,
                                    point,
                                    &mut out,
                                    &mut ids,
                                )?;
                                included = true;
                            }
                            Ok(())
                        },
                    )?;
                    if included {
                        node_keys
                            .push(json!({"source_index":source_index,"id":id,"key":entry.key}));
                    }
                }
            }
        }
        ids.flush()?;
        let manifest = out.finish(Path::new(&req.source), source_points, [has_rgb, true, true])?;
        let mut value = serde_json::to_value(manifest)?;
        value["full_resolution_crop"] = json!(true);
        value["source_stamp"] = json!(source_stamps[0]["stamp"]);
        value["source_sources"] = json!(source_stamps);
        value["source_id_encoding"] =
            json!("source index in bits 56-63, node index in bits 32-55, point index in bits 0-31");
        value["source_nodes"] = json!(node_keys);
        value["crop_bounds"] = json!(req.bounds);
        fs::write(
            output.join("manifest.json"),
            serde_json::to_vec_pretty(&value)?,
        )?;
        Ok(json!({"output":req.output,"manifest":value,"decoded_points":decoded}))
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(output);
    }
    result
}
