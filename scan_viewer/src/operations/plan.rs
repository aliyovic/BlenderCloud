use super::source::{native_source, source, NativeSource, Source};
use super::transform;
use crate::protocol::{PlanRequest, View};
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::HashMap;

fn projected_error(view: &View, bounds: [[f64; 3]; 2], spacing: f64) -> Option<f64> {
    if let Some(section) = &view.clip_matrix {
        let mut outside = [true; 6];
        for x in [bounds[0][0], bounds[1][0]] {
            for y in [bounds[0][1], bounds[1][1]] {
                for z in [bounds[0][2], bounds[1][2]] {
                    let p = transform(
                        section,
                        [x - view.origin[0], y - view.origin[1], z - view.origin[2]],
                    );
                    let distances = [
                        p[3] + p[0],
                        p[3] - p[0],
                        p[3] + p[1],
                        p[3] - p[1],
                        p[3] + p[2],
                        p[3] - p[2],
                    ];
                    for i in 0..6 {
                        outside[i] &= distances[i] < 0.0;
                    }
                }
            }
        }
        if outside.into_iter().any(|value| value) {
            return None;
        }
    }
    let mut outside = [true; 6];
    let mut nearest_w = f64::INFINITY;
    for x in [bounds[0][0], bounds[1][0]] {
        for y in [bounds[0][1], bounds[1][1]] {
            for z in [bounds[0][2], bounds[1][2]] {
                let p = transform(
                    &view.matrix,
                    [x - view.origin[0], y - view.origin[1], z - view.origin[2]],
                );
                let distances = [
                    p[3] + p[0],
                    p[3] - p[0],
                    p[3] + p[1],
                    p[3] - p[1],
                    p[3] + p[2],
                    p[3] - p[2],
                ];
                for i in 0..6 {
                    outside[i] &= distances[i] < 0.0;
                }
                nearest_w = nearest_w.min(p[3]);
            }
        }
    }
    if outside.into_iter().any(|x| x) {
        return None;
    }
    let row_length = |i: usize| {
        view.matrix[i][..3]
            .iter()
            .map(|v| v * v)
            .sum::<f64>()
            .sqrt()
    };
    let scale = (row_length(0) * view.width * 0.5).max(row_length(1) * view.height * 0.5);
    Some(spacing * scale / nearest_w.max(0.01))
}

pub(crate) fn plan(
    req: &PlanRequest,
    sources: &mut HashMap<String, Source>,
    native: &mut HashMap<String, NativeSource>,
) -> Result<Value> {
    let pixel_error = req.pixel_error.max(0.25);
    let mut candidates = Vec::new();
    let mut considered = 0;
    for view in &req.views {
        if view.cache.is_empty() {
            let src = source(sources, &view.source)?;
            if view.class_filter >= 0 {
                src.ensure_class_counts(&view.class_index, req.working_budget_bytes)?;
            }
            for (id, entry) in src.entries.iter().enumerate() {
                considered += 1;
                let filtered_count = if view.class_filter < 0 {
                    entry.point_count as u64
                } else {
                    src.class_counts.as_ref().unwrap()[id]
                        .iter()
                        .find(|(class, _)| i32::from(*class) == view.class_filter)
                        .map_or(0, |(_, count)| u64::from(*count))
                };
                if filtered_count == 0 {
                    continue;
                }
                let spacing = src.spacing / 2_f64.powi(entry.key.l)
                    * (entry.point_count as f64 / filtered_count as f64).sqrt();
                let error = match projected_error(view, src.bounds[id], spacing) {
                    Some(error) => error,
                    None => continue,
                };
                if entry.key.l > 0 && error * 2.0 < pixel_error {
                    continue;
                }
                candidates.push((
                    error,
                    view.source.clone(),
                    String::new(),
                    src.stamp.clone(),
                    id,
                    [entry.key.l, entry.key.x, entry.key.y, entry.key.z],
                    filtered_count,
                    src.bounds[id],
                    view.origin,
                    view.object.clone(),
                    view.class_filter,
                ));
            }
        } else {
            let src = native_source(native, &view.source, &view.cache)?;
            for (id, entry) in src.manifest.nodes.iter().enumerate() {
                considered += 1;
                let filter = if entry.class_counts.is_empty() {
                    -1
                } else {
                    view.class_filter
                };
                let filtered_count = if filter < 0 {
                    entry.count
                } else {
                    entry
                        .class_counts
                        .iter()
                        .find(|(class, _)| i32::from(*class) == filter)
                        .map_or(0, |(_, count)| *count)
                };
                if filtered_count == 0 {
                    continue;
                }
                let spacing = entry.spacing * (entry.count as f64 / filtered_count as f64).sqrt();
                let error = match projected_error(view, entry.bounds, spacing) {
                    Some(error) => error,
                    None => continue,
                };
                if entry.key[0] > 0 && error * 2.0 < pixel_error {
                    continue;
                }
                candidates.push((
                    error,
                    view.source.clone(),
                    view.cache.clone(),
                    src.manifest.stamp.clone(),
                    id,
                    entry.key.map(|v| v as i32),
                    filtered_count,
                    entry.bounds,
                    view.origin,
                    view.object.clone(),
                    filter,
                ));
            }
        }
    }
    candidates.sort_by(|a, b| {
        b.0.total_cmp(&a.0)
            .then_with(|| a.1.cmp(&b.1))
            .then(a.4.cmp(&b.4))
    });
    let mut gpu_remaining = req.gpu_budget.unwrap_or(50_000_000).min(50_000_000);
    let mut selected = Vec::new();
    let mut source_points = 0;
    for candidate in candidates {
        let source_count = candidate.6;
        if source_count > gpu_remaining {
            continue;
        }
        gpu_remaining -= source_count;
        source_points += source_count;
        selected.push(candidate);
    }
    let point_budget = req.budget.min(50_000_000).min(source_points);
    // With a scarce budget, keep the highest-priority regions first.
    if point_budget < selected.len() as u64 {
        selected.truncate(point_budget as usize);
        source_points = selected.iter().map(|candidate| candidate.6).sum();
    }
    let reserved = selected.len() as u64;
    let distributable = point_budget - reserved;
    let weights = source_points - reserved;
    let mut cumulative_source = 0;
    let mut cumulative_drawn = 0;
    let mut nodes = Vec::new();
    for (
        error,
        path,
        cache,
        fingerprint,
        id,
        key,
        source_count,
        bounds,
        origin,
        object,
        class_filter,
    ) in selected
    {
        // Keep every selected region represented, then share the remaining budget proportionally.
        cumulative_source += source_count - 1;
        let target = (cumulative_source * distributable)
            .checked_div(weights)
            .unwrap_or(0);
        let count = 1 + target - cumulative_drawn;
        cumulative_drawn = target;
        if count == 0 {
            continue;
        }
        let drawn_error = error * (source_count as f64 / count as f64).sqrt();
        let (file, cache_origin, has_rgb, total_count) = if cache.is_empty() {
            (String::new(), [0.0; 3], false, source_count)
        } else {
            let src = native_source(native, &path, &cache)?;
            (
                src.manifest.nodes[id].file.clone(),
                src.manifest.origin,
                src.manifest.has_rgb,
                src.manifest.nodes[id].count,
            )
        };
        nodes.push(
            json!({"source":path,"cache":cache,"stamp":fingerprint,"node":id,
            "key":key,
            "source_count":source_count,"count":count,"error":drawn_error,
            "bounds":bounds,"origin":origin,"object":object,
            "file":file,"cache_origin":cache_origin,"has_rgb":has_rgb,
            "total_count":total_count,"class_filter":class_filter}),
        );
    }
    Ok(json!({"nodes":nodes,"points":point_budget,"considered":considered}))
}
