use crate::protocol::Matrix;

mod crop;
mod node;
mod pick;
mod plan;
mod source;

pub(crate) use crop::crop;
pub(crate) use node::{native_node, node};
pub(crate) use pick::pick;
pub(crate) use plan::plan;

fn transform(matrix: &Matrix, p: [f64; 3]) -> [f64; 4] {
    matrix.map(|row| row[0] * p[0] + row[1] * p[1] + row[2] * p[2] + row[3])
}
