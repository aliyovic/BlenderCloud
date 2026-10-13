//! Point ingestion, native caches, and the streaming worker used by Scan Viewer.

#![deny(missing_docs)]
#![forbid(unsafe_code)]

mod cache;
/// Application entry points for the executable and subprocess protocol.
pub mod cli;
mod formats;
mod octree;
mod operations;
mod point;
mod protocol;

use cache::PointSink;
use formats::{parse_e57, parse_las, parse_ply, parse_text};
use point::Point;
