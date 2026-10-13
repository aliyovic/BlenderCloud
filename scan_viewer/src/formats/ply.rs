use super::color;
use crate::cache::PointSink;
use crate::point::Point;
use anyhow::{anyhow, bail, Result};
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

fn ply_width(kind: &str) -> Result<usize> {
    match kind {
        "char" | "int8" | "uchar" | "uint8" => Ok(1),
        "short" | "int16" | "ushort" | "uint16" => Ok(2),
        "int" | "int32" | "uint" | "uint32" | "float" | "float32" => Ok(4),
        "double" | "float64" => Ok(8),
        _ => bail!("Unsupported PLY property type: {kind}"),
    }
}

fn ply_number(bytes: &[u8], kind: &str, big: bool) -> f64 {
    match kind {
        "char" | "int8" => (bytes[0] as i8) as f64,
        "uchar" | "uint8" => bytes[0] as f64,
        "short" | "int16" => {
            (if big {
                i16::from_be_bytes([bytes[0], bytes[1]])
            } else {
                i16::from_le_bytes([bytes[0], bytes[1]])
            }) as f64
        }
        "ushort" | "uint16" => {
            (if big {
                u16::from_be_bytes([bytes[0], bytes[1]])
            } else {
                u16::from_le_bytes([bytes[0], bytes[1]])
            }) as f64
        }
        "int" | "int32" => {
            (if big {
                i32::from_be_bytes(bytes.try_into().unwrap())
            } else {
                i32::from_le_bytes(bytes.try_into().unwrap())
            }) as f64
        }
        "uint" | "uint32" => {
            (if big {
                u32::from_be_bytes(bytes.try_into().unwrap())
            } else {
                u32::from_le_bytes(bytes.try_into().unwrap())
            }) as f64
        }
        "float" | "float32" => {
            (if big {
                f32::from_be_bytes(bytes.try_into().unwrap())
            } else {
                f32::from_le_bytes(bytes.try_into().unwrap())
            }) as f64
        }
        "double" | "float64" => {
            if big {
                f64::from_be_bytes(bytes.try_into().unwrap())
            } else {
                f64::from_le_bytes(bytes.try_into().unwrap())
            }
        }
        _ => unreachable!(),
    }
}

pub(crate) fn parse_ply<S: PointSink>(path: &Path, out: &mut S) -> Result<(u64, [bool; 3])> {
    let mut r = BufReader::new(File::open(path)?);
    let mut line = String::new();
    r.read_line(&mut line)?;
    if line.trim() != "ply" {
        bail!("Not a PLY file");
    }
    let mut format = String::new();
    let mut count = 0;
    let mut in_vertex = false;
    let mut props: Vec<(String, String)> = Vec::new();
    loop {
        line.clear();
        if r.read_line(&mut line)? == 0 {
            bail!("PLY header has no end_header");
        }
        let a: Vec<_> = line.split_whitespace().collect();
        if a.is_empty() {
            continue;
        }
        match a[0] {
            "format" => format = a.get(1).unwrap_or(&"").to_string(),
            "element" => {
                in_vertex = a.get(1) == Some(&"vertex");
                if in_vertex {
                    count = a
                        .get(2)
                        .ok_or_else(|| anyhow!("Missing vertex count"))?
                        .parse()?;
                }
            }
            "property" if in_vertex => {
                if a.len() != 3 {
                    bail!("PLY list vertex properties are unsupported");
                }
                props.push((a[1].to_string(), a[2].to_string()));
            }
            "end_header" => break,
            _ => (),
        }
    }
    let index = |name: &str| props.iter().position(|p| p.1 == name);
    let x = index("x").ok_or_else(|| anyhow!("PLY has no x"))?;
    let y = index("y").ok_or_else(|| anyhow!("PLY has no y"))?;
    let z = index("z").ok_or_else(|| anyhow!("PLY has no z"))?;
    let rgb_idx = [index("red"), index("green"), index("blue")];
    let intensity_idx = index("intensity");
    let class_idx = index("classification");
    let flags = [
        rgb_idx.iter().all(Option::is_some),
        intensity_idx.is_some(),
        class_idx.is_some(),
    ];
    if format == "ascii" {
        for _ in 0..count {
            line.clear();
            if r.read_line(&mut line)? == 0 {
                bail!("PLY ended before vertex count");
            }
            if !out.wants() {
                out.skip();
                continue;
            }
            let a: Vec<_> = line.split_whitespace().collect();
            let number = |i: usize| -> Result<f64> {
                Ok(a.get(i).ok_or_else(|| anyhow!("Short PLY row"))?.parse()?)
            };
            let xyz = [number(x)?, number(y)?, number(z)?];
            let rgb = if flags[0] {
                rgb_idx.map(|i| color(number(i.unwrap()).unwrap_or(0.0)))
            } else {
                [220; 3]
            };
            let intensity = if let Some(i) = intensity_idx {
                number(i)? as f32
            } else {
                0.0
            };
            let class = if let Some(i) = class_idx {
                number(i)? as u8
            } else {
                0
            };
            out.push(Point {
                xyz,
                rgb,
                intensity,
                class,
            })?;
        }
    } else if format == "binary_little_endian" || format == "binary_big_endian" {
        let big = format == "binary_big_endian";
        let mut offsets = Vec::new();
        let mut row_width = 0;
        for (kind, _) in &props {
            offsets.push(row_width);
            row_width += ply_width(kind)?;
        }
        let mut row = vec![0u8; row_width];
        for _ in 0..count {
            r.read_exact(&mut row)?;
            if !out.wants() {
                out.skip();
                continue;
            }
            let number = |i: usize| -> f64 {
                let kind = props[i].0.as_str();
                let start = offsets[i];
                ply_number(&row[start..start + ply_width(kind).unwrap()], kind, big)
            };
            let xyz = [number(x), number(y), number(z)];
            let rgb = if flags[0] {
                rgb_idx.map(|i| {
                    let j = i.unwrap();
                    if ply_width(&props[j].0).unwrap() == 2 {
                        color(number(j) / 257.0)
                    } else {
                        color(number(j))
                    }
                })
            } else {
                [220; 3]
            };
            let intensity = intensity_idx.map(|i| number(i) as f32).unwrap_or(0.0);
            let class = class_idx.map(|i| number(i) as u8).unwrap_or(0);
            out.push(Point {
                xyz,
                rgb,
                intensity,
                class,
            })?;
        }
    } else {
        bail!("Unsupported PLY format: {format}");
    }
    Ok((count, flags))
}
