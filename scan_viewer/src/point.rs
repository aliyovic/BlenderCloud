/// World coordinates use source units. Intensity is normalized to zero through one.
#[derive(Clone, Copy)]
pub(crate) struct Point {
    pub(crate) xyz: [f64; 3],
    pub(crate) rgb: [u8; 3],
    pub(crate) intensity: f32,
    pub(crate) class: u8,
}

impl Point {
    pub(crate) fn from_las(point: las::Point) -> Self {
        Self {
            xyz: [point.x, point.y, point.z],
            rgb: point
                .color
                .map(|color| {
                    [
                        (color.red >> 8) as u8,
                        (color.green >> 8) as u8,
                        (color.blue >> 8) as u8,
                    ]
                })
                .unwrap_or([220; 3]),
            intensity: normalized_intensity(point.intensity),
            class: u8::from(point.classification),
        }
    }
}

pub(crate) fn normalized_intensity(value: u16) -> f32 {
    f32::from(value) / f32::from(u16::MAX)
}
