use super::source::{
    decoder_bytes, native_source, permutation_parameters, source, visit_copc_points,
    visit_native_points, write_packet, NativeSource, Source,
};
use crate::protocol::NodeRequest;
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::collections::HashMap;

pub(crate) fn node(
    req: &NodeRequest,
    sources: &mut HashMap<String, Source>,
) -> Result<(Value, Vec<u8>)> {
    let src = source(sources, &req.source)?;
    if src.stamp != req.stamp {
        bail!("Source changed. Refresh the view to reopen it.");
    }
    let entry = *src.entries.get(req.node).context("Unknown COPC node")?;
    let mut total = usize::try_from(entry.point_count)?;
    if req.class_filter >= 0 {
        total = 0;
        visit_copc_points(src, &entry, req.working_budget_bytes, |_, point| {
            if i32::from(point.class) == req.class_filter {
                total += 1;
            }
            Ok(())
        })?;
        if req.source_count > 0 && usize::try_from(req.source_count)? != total {
            bail!("COPC class index count is stale");
        }
    }
    let decoder = decoder_bytes(&entry)?;
    let (offset, count) = crate::protocol::page_bounds(req, total, decoder)?;
    let mut bytes = vec![0u8; crate::protocol::record_bytes(count)?];
    let mut ordinal = 0;
    let mut emitted = 0;
    let seed = (entry.offset ^ total as u64).wrapping_add(0x9e3779b97f4a7c15);
    let (multiplier, shift) = permutation_parameters(total, seed);
    visit_copc_points(
        src,
        &entry,
        req.working_budget_bytes - bytes.len(),
        |_, point| {
            if req.class_filter >= 0 && i32::from(point.class) != req.class_filter {
                return Ok(());
            }
            let destination =
                ((ordinal as u128 * multiplier as u128 + shift as u128) % total as u128) as usize;
            if destination >= offset && destination < offset + count {
                write_packet(&mut bytes, count, destination - offset, point, req.origin);
                emitted += 1;
            }
            ordinal += 1;
            Ok(())
        },
    )?;
    if ordinal != total || emitted != count {
        bail!("COPC point count changed during decode");
    }
    Ok((
        crate::protocol::page_header(
            req,
            count,
            total,
            src.reader.header().point_format().has_color,
            src.bounds[req.node],
        ),
        bytes,
    ))
}

pub(crate) fn native_node(
    req: &NodeRequest,
    native: &mut HashMap<String, NativeSource>,
) -> Result<(Value, Vec<u8>)> {
    let src = native_source(native, &req.source, &req.cache)?;
    if src.manifest.stamp != req.stamp {
        bail!("Source changed. Rebuild the octree cache.");
    }
    let entry = src
        .manifest
        .nodes
        .get(req.node)
        .context("Unknown octree node")?;
    if !req.file.is_empty()
        && (req.file != entry.file
            || req.node_total_count != entry.count
            || req.cache_origin != src.manifest.origin
            || req.has_rgb != src.manifest.has_rgb
            || req.bounds != entry.bounds)
    {
        bail!("Octree node count or metadata is stale");
    }
    let total_count = usize::try_from(entry.count)?;
    let mut total = 0;
    visit_native_points(
        &src.root,
        &entry.file,
        total_count,
        src.manifest.origin,
        |_, point| {
            if req.class_filter < 0 || i32::from(point.class) == req.class_filter {
                total += 1;
            }
            Ok(())
        },
    )?;
    let declared = if req.class_filter < 0 {
        entry.count
    } else {
        entry
            .class_counts
            .iter()
            .find(|(class, _)| i32::from(*class) == req.class_filter)
            .map_or(0, |(_, count)| *count)
    };
    if usize::try_from(declared)? != total
        || (!req.file.is_empty() && usize::try_from(req.source_count)? != total)
    {
        bail!("Octree class counts are stale");
    }
    let (offset, count) = crate::protocol::page_bounds(req, total, 0)?;
    let mut bytes = vec![0u8; crate::protocol::record_bytes(count)?];
    let mut ordinal = 0;
    visit_native_points(
        &src.root,
        &entry.file,
        total_count,
        src.manifest.origin,
        |_, point| {
            if req.class_filter >= 0 && i32::from(point.class) != req.class_filter {
                return Ok(());
            }
            if ordinal >= offset && ordinal < offset + count {
                write_packet(&mut bytes, count, ordinal - offset, point, req.origin);
            }
            ordinal += 1;
            Ok(())
        },
    )?;
    if ordinal != total {
        bail!("Octree class counts changed during read");
    }
    Ok((
        crate::protocol::page_header(req, count, total, src.manifest.has_rgb, entry.bounds),
        bytes,
    ))
}
