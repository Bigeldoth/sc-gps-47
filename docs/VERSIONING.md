# Versioning policy

SpaceDrive GPS follows [Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`. Choose the increment from all changes since the last published release, including changes already merged into staging.

| Change | Increment | Example |
| --- | --- | --- |
| Incompatible changes to supported interfaces or data formats, without a compatible migration | MAJOR; reset MINOR and PATCH | 1.4.2 → 2.0.0 |
| New compatible functionality, possibly including bug fixes | MINOR; reset PATCH | 1.0.0 → 1.1.0 |
| Compatible corrections only | PATCH | 1.1.0 → 1.1.1 |
| Documentation, tests or internal cleanup without a shipped behavior change | No release increment by themselves | Remain on the prepared version |

The compatibility contract covers documented application behavior and supported configuration, POI exchange and update formats. Preserve existing settings and POIs through compatible migrations. New updater protocols can coexist with old clients through an explicit full-installer fallback; they do not require breaking the existing client contract.

The highest required increment wins. Do not classify a release as PATCH just because its final commits are fixes: it is MINOR if the release also adds features. Do not increment for every commit or PR belonging to the same planned release. Never modify or reuse the contents of an already published version.

The release containing capture-display selection, the optional capture-region debug overlay, support links and updater improvements is **v1.1.0**, following published v1.0.0. Its new compatible features require a MINOR increment. Incrementing the source does not publish a release.

`VERSION` is the single authority. Synchronize its configuration and installer mirrors with:

```powershell
python tools/versioning.py sync --version v1.1.0
python tools/versioning.py check
```

Review and commit the result before building or publishing. Release artifacts and tags use the `v` prefix; `VERSION` and Inno Setup store the numeric version. The local release tooling currently supports stable `X.Y.Z` releases only; prerelease identifiers require a separate tooling change. See [the build and release workflow](BUILD.md).
