//! The CLI must preserve millimetre source precision before local f32 storage.
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("scan-ingestion-{}-{nonce}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn big_endian_ply_preserves_local_coordinates_and_sampled_attributes() {
    let fixture = Fixture::new();
    let source = fixture.0.join("points.ply");
    let cache = fixture.0.join("cache");
    let mut data = b"ply\nformat binary_big_endian 1.0\nelement vertex 3\nproperty double x\nproperty double y\nproperty double z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nproperty float intensity\nproperty uchar classification\nend_header\n".to_vec();
    let points: [([f64; 3], [u8; 3], f32, u8); 3] = [
        ([2689160.125, 1254972.25, 452.125], [255, 0, 0], 0.25, 2),
        ([2689161.25, 1254972.5, 452.25], [0, 255, 0], 0.5, 6),
        ([2689162.375, 1254972.75, 452.375], [0, 0, 255], 0.75, 9),
    ];
    for (xyz, rgb, intensity, class) in points {
        for coordinate in xyz {
            data.extend(coordinate.to_be_bytes());
        }
        data.extend(rgb);
        data.extend(intensity.to_be_bytes());
        data.push(class);
    }
    fs::write(&source, data).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_scan-cache"))
        .arg(&source)
        .arg(&cache)
        .args(["--max-points", "2"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let manifest: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(manifest["source_points"], 3);
    assert_eq!(manifest["retained_points"], 2);
    assert_eq!(manifest["stride"], 2);
    assert_eq!(
        manifest["origin"],
        serde_json::json!([2689000.0, 1254000.0, 0.0])
    );
    let bytes = fs::read(cache.join("positions.f32")).unwrap();
    let positions: Vec<f32> = bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|bytes| f32::from_le_bytes(*bytes))
        .collect();
    assert_eq!(
        positions,
        [160.125, 972.25, 452.125, 162.375, 972.75, 452.375]
    );
    assert_eq!(
        fs::read(cache.join("colors.u8")).unwrap(),
        [255, 0, 0, 0, 0, 255]
    );
    let bytes = fs::read(cache.join("intensity.f32")).unwrap();
    let intensities: Vec<f32> = bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|bytes| f32::from_le_bytes(*bytes))
        .collect();
    assert_eq!(intensities, [0.25, 0.75]);
    assert_eq!(fs::read(cache.join("classification.u8")).unwrap(), [2, 9]);
}
