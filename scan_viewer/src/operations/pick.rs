use super::crop::intersects;
use super::source::{
    native_source, source, visit_copc_points, visit_native_points, NativeSource, Source,
};
use super::transform;
use crate::point::Point;
use crate::protocol::{PickRequest, PickSource};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::collections::HashMap;

fn ray_meets_bounds(
    origin: [f64; 3],
    direction: [f64; 3],
    bounds: [[f64; 3]; 2],
    radius: f64,
    max_distance: f64,
) -> bool {
    let (mut near, mut far) = (0.0f64, max_distance);
    for axis in 0..3 {
        let low = bounds[0][axis] - radius;
        let high = bounds[1][axis] + radius;
        if direction[axis].abs() < 1e-12 {
            if origin[axis] < low || origin[axis] > high {
                return false;
            }
        } else {
            let a = (low - origin[axis]) / direction[axis];
            let b = (high - origin[axis]) / direction[axis];
            near = near.max(a.min(b));
            far = far.min(a.max(b));
            if far < near {
                return false;
            }
        }
    }
    true
}

#[derive(Default)]
struct PlaneFit {
    count: usize,
    mean: [f64; 3],
    covariance: [[f64; 3]; 3],
}

impl PlaneFit {
    fn push(&mut self, point: [f64; 3]) {
        self.count += 1;
        let delta: [f64; 3] = std::array::from_fn(|axis| point[axis] - self.mean[axis]);
        for (mean, value) in self.mean.iter_mut().zip(delta) {
            *mean += value / self.count as f64;
        }
        for (i, row) in self.covariance.iter_mut().enumerate() {
            for (j, value) in row.iter_mut().enumerate() {
                *value += delta[i] * (point[j] - self.mean[j]);
            }
        }
    }

    fn normal(&self, ray: [f64; 3]) -> Option<[f64; 3]> {
        if self.count < 3 {
            return None;
        }
        let mut covariance = self.covariance;
        let mut vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
        for _ in 0..24 {
            let (mut p, mut q) = (0, 1);
            for (i, j) in [(0, 1), (0, 2), (1, 2)] {
                if covariance[i][j].abs() > covariance[p][q].abs() {
                    (p, q) = (i, j);
                }
            }
            if covariance[p][q].abs() < 1e-12 {
                break;
            }
            let angle = 0.5 * (2.0 * covariance[p][q]).atan2(covariance[q][q] - covariance[p][p]);
            let (s, c) = angle.sin_cos();
            for (row, vector) in covariance.iter_mut().zip(vectors.iter_mut()) {
                let (a, b) = (row[p], row[q]);
                row[p] = c * a - s * b;
                row[q] = s * a + c * b;
                let (a, b) = (vector[p], vector[q]);
                vector[p] = c * a - s * b;
                vector[q] = s * a + c * b;
            }
            let old_p = covariance[p];
            let old_q = covariance[q];
            for (value, (a, b)) in covariance[p].iter_mut().zip(old_p.into_iter().zip(old_q)) {
                *value = c * a - s * b;
            }
            for (value, (a, b)) in covariance[q].iter_mut().zip(old_p.into_iter().zip(old_q)) {
                *value = s * a + c * b;
            }
        }
        let axis = (0..3).min_by(|a, b| covariance[*a][*a].total_cmp(&covariance[*b][*b]))?;
        let mut normal = [vectors[0][axis], vectors[1][axis], vectors[2][axis]];
        let length = normal.iter().map(|v| v * v).sum::<f64>().sqrt();
        if length < 1e-12 {
            return None;
        }
        for value in &mut normal {
            *value /= length;
        }
        if (0..3).map(|i| normal[i] * ray[i]).sum::<f64>() > 0.0 {
            for value in &mut normal {
                *value = -*value;
            }
        }
        Some(normal)
    }
}

#[derive(Clone, Copy)]
struct PickHit {
    squared: f64,
    distance: f64,
    source_index: usize,
    xyz: [f64; 3],
    id: u64,
}

fn allowed_pick_point(spec: &PickSource, point: Point) -> bool {
    if spec.class_filter >= 0 && i32::from(point.class) != spec.class_filter {
        return false;
    }
    if let Some(matrix) = &spec.clip_matrix {
        let local = std::array::from_fn(|i| point.xyz[i] - spec.origin[i]);
        let q = transform(matrix, local);
        if q[..3].iter().any(|v| v.abs() > 1.0 + 1e-9) {
            return false;
        }
    }
    true
}

struct PickSelection<'a> {
    spec: &'a PickSource,
    source_index: usize,
    direction: [f64; 3],
    radius: f64,
    best: &'a mut Option<PickHit>,
}

impl PickSelection<'_> {
    fn consider(&mut self, node_id: usize, point_index: usize, point: Point) -> Result<()> {
        if !allowed_pick_point(self.spec, point) {
            return Ok(());
        }
        if node_id > 0x00ff_ffff || point_index > u32::MAX as usize {
            bail!("Pick source ID exceeds protocol limits");
        }
        let delta: [f64; 3] = std::array::from_fn(|i| point.xyz[i] - self.spec.ray_origin[i]);
        let distance = (0..3).map(|i| delta[i] * self.direction[i]).sum::<f64>();
        if distance < 0.0 || distance > self.spec.max_distance {
            return Ok(());
        }
        let squared = (0..3)
            .map(|i| (delta[i] - distance * self.direction[i]).powi(2))
            .sum::<f64>();
        if squared > self.radius * self.radius {
            return Ok(());
        }
        if self.best.is_none_or(|best| {
            squared < best.squared || (squared == best.squared && distance < best.distance)
        }) {
            let id =
                ((self.source_index as u64) << 56) | ((node_id as u64) << 32) | point_index as u64;
            *self.best = Some(PickHit {
                squared,
                distance,
                source_index: self.source_index,
                xyz: point.xyz,
                id,
            });
        }
        Ok(())
    }
}

pub(crate) fn pick(
    req: &PickRequest,
    sources: &mut HashMap<String, Source>,
    native: &mut HashMap<String, NativeSource>,
) -> Result<Value> {
    if req.pick_sources.is_empty() {
        bail!("Pick needs at least one source");
    }
    if req.pick_sources.len() > 256 {
        bail!("Pick supports at most 256 sources");
    }
    if !req.pick_radius.is_finite() || !req.plane_radius.is_finite() {
        bail!("Invalid pick radius");
    }
    let radius = req.pick_radius.clamp(0.001, 100.0);
    let plane_radius = req.plane_radius.clamp(radius, 100.0);
    let mut best = None;
    let mut decoded = 0u64;
    for (source_index, spec) in req.pick_sources.iter().enumerate() {
        let length = spec.ray_direction.iter().map(|v| v * v).sum::<f64>().sqrt();
        if !length.is_finite()
            || length < 1e-12
            || !spec.max_distance.is_finite()
            || spec.max_distance <= 0.0
            || !spec.ray_origin.iter().all(|value| value.is_finite())
        {
            bail!("Invalid pick ray");
        }
        let direction = spec.ray_direction.map(|v| v / length);
        let mut selection = PickSelection {
            spec,
            source_index,
            direction,
            radius,
            best: &mut best,
        };
        if spec.cache.is_empty() {
            let src = source(sources, &spec.source)?;
            let entries = src.entries.clone();
            for (node_id, entry) in entries.iter().enumerate() {
                if !ray_meets_bounds(
                    spec.ray_origin,
                    direction,
                    src.bounds[node_id],
                    radius,
                    spec.max_distance,
                ) {
                    continue;
                }
                visit_copc_points(src, entry, req.working_budget_bytes, |index, point| {
                    selection.consider(node_id, index, point)
                })?;
                decoded += u64::try_from(entry.point_count)?;
            }
        } else {
            let src = native_source(native, &spec.source, &spec.cache)?;
            for (node_id, entry) in src.manifest.nodes.iter().enumerate() {
                if !ray_meets_bounds(
                    spec.ray_origin,
                    direction,
                    entry.bounds,
                    radius,
                    spec.max_distance,
                ) {
                    continue;
                }
                visit_native_points(
                    &src.root,
                    &entry.file,
                    usize::try_from(entry.count)?,
                    src.manifest.origin,
                    |index, point| selection.consider(node_id, index, point),
                )?;
                decoded += entry.count;
            }
        }
    }
    let Some(hit) = best else {
        return Ok(json!({"found":false,"decoded_points":decoded}));
    };
    let spec = &req.pick_sources[hit.source_index];
    let bounds = [
        hit.xyz.map(|value| value - plane_radius),
        hit.xyz.map(|value| value + plane_radius),
    ];
    let mut fit = PlaneFit::default();
    let mut add_neighbor = |_: usize, point: Point| -> Result<()> {
        let squared = (0..3)
            .map(|i| (point.xyz[i] - hit.xyz[i]).powi(2))
            .sum::<f64>();
        if squared <= plane_radius * plane_radius && allowed_pick_point(spec, point) {
            fit.push(point.xyz);
        }
        Ok(())
    };
    if spec.cache.is_empty() {
        let src = source(sources, &spec.source)?;
        let entries = src.entries.clone();
        for (id, entry) in entries.iter().enumerate() {
            if intersects(src.bounds[id], bounds) {
                visit_copc_points(src, entry, req.working_budget_bytes, &mut add_neighbor)?;
                decoded += u64::try_from(entry.point_count)?;
            }
        }
    } else {
        let src = native_source(native, &spec.source, &spec.cache)?;
        for entry in &src.manifest.nodes {
            if intersects(entry.bounds, bounds) {
                visit_native_points(
                    &src.root,
                    &entry.file,
                    usize::try_from(entry.count)?,
                    src.manifest.origin,
                    &mut add_neighbor,
                )?;
                decoded += entry.count;
            }
        }
    }
    Ok(
        json!({"found":true,"source":spec.source,"source_index":hit.source_index,"source_id":hit.id,
        "xyz":hit.xyz,"normal":fit.normal(spec.ray_direction),"plane_points":fit.count,
        "ray_distance":hit.squared.sqrt(),"decoded_points":decoded}),
    )
}
