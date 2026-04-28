# Security policy

## Reporting a vulnerability

If you find a security issue in the Chipzen SDK or believe the
protocol spec contains a flaw with security implications, please
report it privately rather than opening a public issue.

Email: **security@chipzen.ai**

Include:
- A description of the issue and the impact you observed.
- Steps to reproduce.
- The affected SDK version / commit SHA.
- Your preferred contact info for follow-up.

We will acknowledge within 5 business days during the alpha period.
A coordinated-disclosure window of 30 days is the default; we may
request a longer window for issues that require platform-side
changes coordinated with `chipzen-ai/Chipzen`.

## Scope

In scope for this repo:
- Vulnerabilities in the SDK code itself (starters, reference bot,
  shared client utilities).
- Flaws in the wire-protocol specification that would let a
  malicious client compromise the platform.

Out of scope (route to support@chipzen.ai or report directly to
the platform):
- Issues with `chipzen-ai/Chipzen` platform code, hosting, or
  deployed services. We can route them, but they are not fixed
  here.
- Issues with third-party services Chipzen integrates with.

## No bounty during alpha

We do not run a paid bug-bounty program during alpha. Reporters
of significant issues will be credited in a future security
acknowledgements section unless they request anonymity.
