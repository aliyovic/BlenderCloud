use super::color;
use crate::cache::PointSink;
use crate::point::Point;
use anyhow::Result;
use std::io::BufRead;

pub(crate) fn parse_text<R: BufRead, S: PointSink>(
    mut reader: R,
    out: &mut S,
    asc_header: bool,
) -> Result<[bool; 3]> {
    let mut line = String::new();
    let mut first = true;
    let mut flags = [false; 3];
    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        let s = line.trim();
        if s.is_empty() || s.starts_with('#') || s.starts_with("//") {
            continue;
        }
        if first {
            first = false;
            if asc_header && s.parse::<u64>().is_ok() {
                continue;
            }
            if s.split(|c: char| c.is_whitespace() || c == ',' || c == ';')
                .find(|x| !x.is_empty())
                .unwrap_or("")
                .parse::<f64>()
                .is_err()
            {
                continue;
            }
        }
        if !out.wants() {
            out.skip();
            continue;
        }
        let a: Vec<&str> = s
            .split(|c: char| c.is_whitespace() || c == ',' || c == ';')
            .filter(|x| !x.is_empty())
            .collect();
        if a.len() < 3 {
            continue;
        }
        let xyz = [a[0].parse()?, a[1].parse()?, a[2].parse()?];
        let rgb = if a.len() >= 6 {
            flags[0] = true;
            [
                color(a[3].parse()?),
                color(a[4].parse()?),
                color(a[5].parse()?),
            ]
        } else {
            [220, 220, 220]
        };
        let intensity = if a.len() > 6 {
            flags[1] = true;
            a[6].parse()?
        } else {
            0.0
        };
        let class = if a.len() > 7 {
            flags[2] = true;
            a[7].parse::<f64>()? as u8
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
    Ok(flags)
}
