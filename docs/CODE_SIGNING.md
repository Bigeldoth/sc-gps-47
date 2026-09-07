# Code Signing Policy

This document describes how SpaceDrive GPS release binaries are produced, signed
and verified. It exists so that users and distributors can establish what a
SpaceDrive GPS signature does and does not guarantee.

## Current status

Release binaries are **not yet code-signed**. Windows SmartScreen therefore shows
an "unknown publisher" warning on download. An application to the
[SignPath Foundation](https://signpath.org/) free code signing programme for
open source projects is in progress; this document describes the policy that
applies once a certificate is granted.

Until then, users can verify a download by comparing its SHA-256 against the
value published on the release page.

## Signed artifacts

| Artifact | Description |
|---|---|
| `SpaceDrive-Setup-vX.Y.Z.exe` | Inno Setup installer, the only officially distributed binary |
| `spaceDrive.exe` | PyInstaller-built application executable, contained in the installer |

No other file is signed. In particular, SpaceDrive GPS downloads the
[UB-Mannheim Tesseract OCR](https://github.com/UB-Mannheim/tesseract) installer
at install time; that third-party binary carries its own publisher's signature,
not ours. Its integrity is pinned by an expected SHA-256 in the installer, which
refuses to execute it on mismatch.

## Build process

Release binaries are built by GitHub Actions from the public repository, on
GitHub-hosted runners only:

- **Repository:** https://github.com/Bigeldoth/sc-gps-47
- **Workflow:** [`.github/workflows/build.yml`](../.github/workflows/build.yml)
- **Runner:** `windows-latest` (GitHub-hosted)
- **Trigger:** pushing a `vX.Y.Z` tag
- **Steps:** PyInstaller one-folder bundle, then Inno Setup compilation via
  [`tools/build_installer.ps1`](../tools/build_installer.ps1)

The build is reproducible locally with the same script; see
[`docs/BUILD.md`](BUILD.md).

## Signing process

Signing is performed by [SignPath.io](https://signpath.io/) using a certificate
provided by the SignPath Foundation. The signing key is held in SignPath's HSM
and is never available to the project or its maintainers.

- Signing requests are submitted from the GitHub Actions workflow above.
- **Origin verification is enabled**: SignPath validates that the submitted
  artifact is the output of an automated build of this repository before signing.
  Artifacts built anywhere else, including on a maintainer's machine, cannot be
  signed.
- Every release requires **manual approval** before a signature is issued.

Because the certificate is issued by the SignPath Foundation, Windows will
display **SignPath Foundation** as the verified publisher rather than the project
name. This is expected and is not an indication of tampering.

## What a signature guarantees

A valid signature confirms that the binary is an unmodified, automated build of
the source code in the repository above, at the tagged commit, and that a
maintainer approved its release.

It does **not** constitute an endorsement, security audit, or warranty by the
SignPath Foundation. SpaceDrive GPS is provided under the GNU General Public
License v3.0 with no warranty; see [`LICENSE.txt`](../LICENSE.txt).

## Verifying a download

Check the publisher in the file's Properties dialog, under **Digital
Signatures**, or from PowerShell:

```powershell
Get-AuthenticodeSignature .\SpaceDrive-Setup-v1.0.0.exe | Format-List Status, SignerCertificate
```

`Status` must be `Valid`. Independently, compare the file hash against the value
published on the release page:

```powershell
Get-FileHash .\SpaceDrive-Setup-v1.0.0.exe -Algorithm SHA256
```

## Distribution channels

Official downloads are served from:

- **SpaceDrive Community Hub** — https://spacedrive.padek-interactive.tech
- **GitHub Releases** — https://github.com/Bigeldoth/sc-gps-47/releases

Binaries obtained anywhere else are not covered by this policy.

## Reporting a problem

Report a suspected tampered or misattributed binary, or a signature that fails to
validate, via a [GitHub issue](https://github.com/Bigeldoth/sc-gps-47/issues).
For anything you believe should not be disclosed publicly, use GitHub's private
vulnerability reporting on the same repository.
