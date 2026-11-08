// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Command, Stdio};

/// Best effort: start the muse-linux backend if it is installed and
/// not already running. The GUI also works against a backend started
/// by hand or as a service; failure here is not fatal.
fn spawn_backend() {
    let port = std::env::var("MUSE_LINUX_PORT").unwrap_or_else(|_| "8770".into());
    let _ = Command::new("muse-linux")
        .args(["serve", "--port", &port])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

fn main() {
    spawn_backend();
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running muse-linux");
}
