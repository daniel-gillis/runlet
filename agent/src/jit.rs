//! The JIT (just-in-time) runner configuration.
//!
//! Verified against a real `generate-jitconfig` response, not inferred:
//!
//! ```text
//! base64(                                  <- the --jitconfig argument
//!   { ".runner":                 base64(json),
//!     ".credentials":            base64(json),
//!     ".credentials_rsaparams":  base64(json) })
//! ```
//!
//! Three documents, each base64-encoded *inside* the outer base64. Getting this
//! wrong is not merely a parsing inconvenience: `.credentials_rsaparams` carries
//! the RSA key the server uses to encrypt job messages, so an agent that ignores
//! it can connect but can never read a job.
//!
//! Fields observed in a live config: `Ephemeral: true` (a JIT runner serves
//! exactly one job) and `UseV2Flow: true` with a separate `ServerUrlV2` — the
//! modern broker flow, which is the one runlet must speak.

use base64::Engine as _;
use serde::{Deserialize, Deserializer};

/// GitHub encodes ids as **strings** in the JIT document (`"AgentId":"168"`),
/// while the typed model elsewhere treats them as numbers. Accept both: the
/// wire format is the one that matters, and a real config is what proved it.
fn lenient_u64<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Raw {
        Number(u64),
        Text(String),
    }
    match Raw::deserialize(deserializer)? {
        Raw::Number(n) => Ok(n),
        Raw::Text(s) => s.trim().parse().map_err(serde::de::Error::custom),
    }
}

/// The three documents GitHub mints per runner.
#[derive(Debug, Deserialize)]
pub struct JitConfig {
    #[serde(rename = ".runner")]
    pub runner: RunnerSettings,
    #[serde(rename = ".credentials")]
    pub credentials: Credentials,
    #[serde(rename = ".credentials_rsaparams")]
    pub rsa: RsaParams,
}

/// The JIT document stringifies booleans: `"Ephemeral":"True"`, capital T.
/// Accept real booleans too, so the parser survives either serialiser.
fn lenient_bool<'de, D: Deserializer<'de>>(deserializer: D) -> Result<bool, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Raw {
        Bool(bool),
        Text(String),
    }
    match Raw::deserialize(deserializer)? {
        Raw::Bool(b) => Ok(b),
        Raw::Text(s) => match s.trim().to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" => Ok(true),
            "false" | "0" | "no" | "" => Ok(false),
            other => Err(serde::de::Error::custom(format!("not a boolean: {other}"))),
        },
    }
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct RunnerSettings {
    #[serde(deserialize_with = "lenient_u64")]
    pub agent_id: u64,
    pub agent_name: String,
    #[serde(deserialize_with = "lenient_u64")]
    pub pool_id: u64,
    /// Registration/queue host.
    pub server_url: String,
    /// Broker host for the V2 flow; empty on older configs.
    #[serde(default)]
    pub server_url_v2: String,
    #[serde(default)]
    pub github_url: String,
    #[serde(default)]
    pub work_folder: String,
    /// A JIT runner is single-use by construction.
    #[serde(default, deserialize_with = "lenient_bool")]
    pub ephemeral: bool,
    /// When true the agent must use `server_url_v2`, not `server_url`.
    #[serde(default, deserialize_with = "lenient_bool")]
    pub use_v2_flow: bool,
}

#[derive(Deserialize, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct Credentials {
    /// Observed: "OAuth".
    pub scheme: String,
    /// An OAuth *client*, not a bearer token: the agent exchanges these for an
    /// access token. Verified against a live config.
    pub data: OAuthClient,
}

#[derive(Deserialize, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct OAuthClient {
    pub client_id: String,
    /// Endpoint that issues the runner's access token.
    pub authorization_url: String,
}

/// RSA private key used to unwrap the per-session message encryption key.
#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct RsaParams {
    pub modulus: String,
    pub exponent: String,
    pub d: String,
    pub p: String,
    pub q: String,
    pub dp: String,
    pub dq: String,
    #[serde(rename = "inverseQ")]
    pub inverse_q: String,
}

#[derive(Debug)]
pub enum JitError {
    NotBase64(&'static str, String),
    NotJson(&'static str, String),
    MissingDocument(&'static str),
}

impl std::fmt::Display for JitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotBase64(which, m) => write!(f, "{which} is not valid base64: {m}"),
            Self::NotJson(which, m) => write!(f, "{which} is not valid json: {m}"),
            Self::MissingDocument(which) => write!(f, "jit config has no {which} document"),
        }
    }
}

impl std::error::Error for JitError {}

impl std::fmt::Debug for Credentials {
    /// The client id is credential material; show only that it exists.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Credentials")
            .field("scheme", &self.scheme)
            .field("client_id", &"<redacted>")
            .field("authorization_url", &self.data.authorization_url)
            .finish()
    }
}

fn b64(value: &str, which: &'static str) -> Result<Vec<u8>, JitError> {
    base64::engine::general_purpose::STANDARD
        .decode(value.trim())
        .map_err(|e| JitError::NotBase64(which, e.to_string()))
}

/// Decode the outer base64 and each of the three inner documents.
pub fn decode(raw: &str) -> Result<JitConfig, JitError> {
    let outer: serde_json::Map<String, serde_json::Value> =
        serde_json::from_slice(&b64(raw, "jit config")?)
            .map_err(|e| JitError::NotJson("jit config", e.to_string()))?;

    // Check every document exists *before* parsing any of them: otherwise a
    // malformed .runner masks a missing .credentials and the operator is sent
    // looking in the wrong place.
    for key in [".runner", ".credentials", ".credentials_rsaparams"] {
        if outer.get(key).and_then(serde_json::Value::as_str).is_none() {
            return Err(JitError::MissingDocument(key));
        }
    }

    let document = |key: &'static str| -> Result<serde_json::Value, JitError> {
        let value = outer
            .get(key)
            .and_then(serde_json::Value::as_str)
            .ok_or(JitError::MissingDocument(key))?;
        serde_json::from_slice(&b64(value, key)?).map_err(|e| JitError::NotJson(key, e.to_string()))
    };

    Ok(JitConfig {
        runner: serde_json::from_value(document(".runner")?)
            .map_err(|e| JitError::NotJson(".runner", e.to_string()))?,
        credentials: serde_json::from_value(document(".credentials")?)
            .map_err(|e| JitError::NotJson(".credentials", e.to_string()))?,
        rsa: serde_json::from_value(document(".credentials_rsaparams")?)
            .map_err(|e| JitError::NotJson(".credentials_rsaparams", e.to_string()))?,
    })
}

impl JitConfig {
    /// Host the agent should talk to, honouring the V2 flow.
    #[must_use]
    pub fn endpoint(&self) -> &str {
        if self.runner.use_v2_flow && !self.runner.server_url_v2.is_empty() {
            &self.runner.server_url_v2
        } else {
            &self.runner.server_url
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RSA: &str = r#"{"d":"dd","dp":"dp","dq":"dq","exponent":"AQAB",
                          "inverseQ":"iq","modulus":"mm","p":"pp","q":"qq"}"#;

    fn document(json: &str) -> String {
        base64::engine::general_purpose::STANDARD.encode(json)
    }

    /// Build a config with the exact shape GitHub produces.
    fn encode(runner: &str, credentials: &str, rsa: &str) -> String {
        let payload = format!(
            r#"{{".runner":"{}",".credentials":"{}",".credentials_rsaparams":"{}"}}"#,
            document(runner),
            document(credentials),
            document(rsa)
        );
        base64::engine::general_purpose::STANDARD.encode(payload)
    }

    fn sample() -> String {
        encode(
            r#"{"AgentId":168,"AgentName":"runlet-probe","DisableUpdate":true,
                "Ephemeral":true,"PoolId":1,"PoolName":null,
                "ServerUrl":"https://pipelines.example/abc",
                "WorkFolder":"_work","GitHubUrl":"https://github.com/x/y",
                "ServerUrlV2":"https://broker.example/xyz","UseV2Flow":true}"#,
            r#"{"Scheme":"OAuth","Data":{"ClientId":"client-abc","AuthorizationUrl":"https://auth.example/token"}}"#,
            RSA,
        )
    }

    #[test]
    fn decodes_the_real_three_document_shape() {
        let config = decode(&sample()).expect("valid config");
        assert_eq!(config.runner.agent_id, 168);
        assert_eq!(config.runner.agent_name, "runlet-probe");
        assert_eq!(config.runner.pool_id, 1);
        assert!(config.runner.ephemeral, "JIT runners are single-use");
        assert!(config.runner.use_v2_flow);
        assert_eq!(config.credentials.scheme, "OAuth");
        assert_eq!(config.credentials.data.client_id, "client-abc");
        assert!(config.credentials.data.authorization_url.ends_with("/token"));
        assert_eq!(config.rsa.exponent, "AQAB");
    }

    #[test]
    fn accepts_ids_encoded_as_strings_like_the_live_api() {
        // Copied from a real generate-jitconfig response: ids are strings.
        let raw = encode(
            r#"{"AgentId":"168","AgentName":"runlet-probe","DisableUpdate":true,
                "Ephemeral":true,"PoolId":"1","PoolName":null,
                "ServerUrl":"https://pipelines.example/abc","WorkFolder":"_work",
                "GitHubUrl":"https://github.com/x/y","ServerUrlV2":"https://broker.example/xyz",
                "UseV2Flow":true}"#,
            r#"{"Scheme":"OAuth","Data":{"ClientId":"c","AuthorizationUrl":"https://a"}}"#,
            RSA,
        );
        let config = decode(&raw).expect("strings must be accepted");
        assert_eq!(config.runner.agent_id, 168);
        assert_eq!(config.runner.pool_id, 1);
    }

    #[test]
    fn accepts_booleans_encoded_as_capitalised_strings() {
        // The live document says "Ephemeral":"True".
        let raw = encode(
            r#"{"AgentId":"168","AgentName":"runlet-probe","DisableUpdate":"True",
                "Ephemeral":"True","PoolId":"1","ServerUrl":"https://p",
                "ServerUrlV2":"https://b","UseV2Flow":"True"}"#,
            r#"{"Scheme":"OAuth","Data":{"ClientId":"c","AuthorizationUrl":"https://a"}}"#,
            RSA,
        );
        let config = decode(&raw).expect("string booleans must be accepted");
        assert!(config.runner.ephemeral);
        assert!(config.runner.use_v2_flow);
        assert_eq!(config.endpoint(), "https://b");
    }

    #[test]
    fn endpoint_prefers_v2_when_enabled() {
        assert_eq!(decode(&sample()).unwrap().endpoint(), "https://broker.example/xyz");
    }

    #[test]
    fn endpoint_falls_back_to_v1() {
        let raw = encode(
            r#"{"AgentId":1,"AgentName":"a","PoolId":2,"ServerUrl":"https://v1.example",
                "ServerUrlV2":"https://v2.example","UseV2Flow":false}"#,
            r#"{"Scheme":"OAuth","Data":{"ClientId":"c","AuthorizationUrl":"https://a"}}"#,
            RSA,
        );
        assert_eq!(decode(&raw).unwrap().endpoint(), "https://v1.example");
    }

    #[test]
    fn credential_never_appears_in_debug_output() {
        let config = decode(&sample()).unwrap();
        let rendered = format!("{:?}", config.credentials);
        assert!(!rendered.contains("client-abc"), "client id leaked: {rendered}");
        assert!(rendered.contains("<redacted>"));
    }

    #[test]
    fn missing_document_is_reported_by_name() {
        let payload = format!(r#"{{".runner":"{}"}}"#, document("{}"));
        let raw = base64::engine::general_purpose::STANDARD.encode(payload);
        match decode(&raw) {
            Err(JitError::MissingDocument(name)) => assert_eq!(name, ".credentials"),
            other => panic!("expected MissingDocument, got {other:?}"),
        }
    }

    #[test]
    fn rejects_garbage() {
        assert!(matches!(decode("!!! not base64"), Err(JitError::NotBase64(..))));
        let raw = base64::engine::general_purpose::STANDARD.encode("not json");
        assert!(matches!(decode(&raw), Err(JitError::NotJson(..))));
    }
}
