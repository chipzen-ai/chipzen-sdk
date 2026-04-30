//! CLI internals for `chipzen-sdk`. Exposed as a library so integration
//! tests can drive `scaffold_bot` and `validate_bot` directly without
//! shelling out to the binary.

pub mod cli;
pub mod scaffold;
pub mod validate;

pub use scaffold::{scaffold_bot, ScaffoldOptions, _DOCKERFILE_TEMPLATE_FOR_TEST};
pub use validate::{
    validate_bot, Severity, ValidateOptions, ValidationResult, DEFAULT_MAX_UPLOAD_BYTES,
};
