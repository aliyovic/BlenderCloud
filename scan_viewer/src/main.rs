fn main() -> anyhow::Result<()> {
    scan_cache::cli::run(&std::env::args().collect::<Vec<_>>())
}
