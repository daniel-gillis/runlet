//! The JIT (just-in-time) runner configuration.
//!
//! GitHub mints a single-use configuration for one runner, base64-encoded, and
//! passes it to the agent at startup. That is exactly the ephemeral semantics
//! runlet wants: no registration API call on the hot path, no reusable
//! credential on disk, one job per process.
//!
//! Field names are taken from the runner's own JIT document. Parsing is
//! deliberately tolerant: unknown fields are ignored, and only the four values
//! the agent cannot start without are required. A GitHub-side addition should not
//! break a running deployment.

use base64::Engine as _;
use serde::Deserialize;

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct JitConfig {
    /// Server the agent reports to (the pipelines host, not github.com).
    pub server_url: String,
    /// Pool the runner belongs to.
    pub pool_id: u64,
    /// Identity assigned to this runner.
    pub agent_id: u64,
    /// Human-readable runner name, used in logs.
    #[serde(default)]
    pub agent_name: String,
    /// Working directory for job checkouts, when the server specifies one.
    #[serde(default)]
    pub work_folder: String,
}

#[derive(Debug)]
pub enum JitError {
    NotBase64(String),
    NotJson(String),
}

impl std::fmt::Display for JitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotBase64(m) => write!(f, "jit config is not valid base64: {m}"),
            Self::NotJson(m) => write!(f, "jit config is not valid json: {m}"),
        }
    }
}

impl std::error::Error for JitError {}

/// Decode a `--jitconfig` value into its parts.
pub fn decode(raw: &str) -> Result<JitConfig, JitError> {
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(raw.trim())
        .map_err(|e| JitError::NotBase64(e.to_string()))?;
    serde_json::from_slice(&bytes).map_err(|e| JitError::NotJson(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn encode(json: &str) -> String {
        base64::engine::general_purpose::STANDARD.encode(json)
    }

    #[test]
    fn decodes_the_expected_document() {
        let raw = encode(
            r#"{"AgentId":42,"AgentName":"runlet-1","PoolId":7,
                "ServerUrl":"https://pipelines.actions.githubusercontent.com/abc",
                "WorkFolder":"/_work"}"#,
        );
        let config = decode(&raw).expect("valid config");
        assert_eq!(config.agent_id, 42);
        assert_eq!(config.agent_name, "runlet-1");
        assert_eq!(config.pool_id, 7);
        assert!(config.server_url.ends_with("/abc"));
        assert_eq!(config.work_folder, "/_work");
    }

    #[test]
    fn tolerates_unknown_fields() {
        // GitHub will add fields; that must not break a running deployment.
        let raw = encode(
            r#"{"AgentId":1,"PoolId":2,"ServerUrl":"https://x","SomeFutureField":{"a":1}}"#,
        );
        assert!(decode(&raw).is_ok());
    }

    #[test]
    fn optional_fields_default() {
        let raw = encode(r#"{"AgentId":1,"PoolId":2,"ServerUrl":"https://x"}"#);
        let config = decode(&raw).unwrap();
        assert_eq!(config.agent_name, "");
        assert_eq!(config.work_folder, "");
    }

    #[test]
    fn rejects_missing_required_fields() {
        let raw = encode(r#"{"AgentName":"no ids"}"#);
        assert!(matches!(decode(&raw), Err(JitError::NotJson(_))));
    }

    #[test]
    fn rejects_garbage() {
        assert!(matches!(decode("not base64 !!!"), Err(JitError::NotBase64(_))));
        let raw = encode("this is not json");
        assert!(matches!(decode(&raw), Err(JitError::NotJson(_))));
    }

    #[test]
    fn ignores_surrounding_whitespace() {
        let raw = format!("  {}  \n", encode(r#"{"AgentId":1,"PoolId":2,"ServerUrl":"https://x"}"#));
        assert!(decode(&raw).is_ok());
    }
}
