//! runlet-agent — a minimal, fast GitHub Actions agent.
//!
//! Milestone 1 is deliberately narrow: decode a JIT configuration and prove the
//! pieces the hot path needs. The protocol surface is documented in
//! `../docs/agent-protocol.md`; the parts not yet implemented are marked
//! *unverified* there rather than guessed at here.

mod jit;

use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--jitconfig") => match args.next() {
            Some(raw) => match jit::decode(&raw) {
                Ok(config) => {
                    println!(
                        "runlet-agent {} pool={} agent={} name={} ephemeral={} endpoint={}",
                        env!("CARGO_PKG_VERSION"),
                        config.runner.pool_id,
                        config.runner.agent_id,
                        config.runner.agent_name,
                        config.runner.ephemeral,
                        config.endpoint()
                    );
                    // Surfaced without values: the agent needs both the bearer
                    // credential and the RSA key to read an encrypted job message.
                    // Base64 length -> bytes, so the modulus size is reported
                    // honestly rather than as a guess at bit strength.
                    let modulus_bytes = config.rsa.modulus.len() * 3 / 4;
                    println!(
                        "  auth={} client={} rsa_modulus={} bytes",
                        config.credentials.scheme,
                        config.credentials.data.client_id.len(),
                        modulus_bytes
                    );
                    ExitCode::SUCCESS
                }
                Err(err) => {
                    eprintln!("runlet-agent: {err}");
                    ExitCode::FAILURE
                }
            },
            None => {
                eprintln!("runlet-agent: --jitconfig requires a value");
                ExitCode::from(2)
            }
        },
        Some("--version") | Some("-V") => {
            println!("runlet-agent {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("usage: runlet-agent --jitconfig <base64> | --version");
            ExitCode::from(2)
        }
    }
}
