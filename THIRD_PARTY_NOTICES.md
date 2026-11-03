# Third-party notices

The MIT license in this repository covers project-authored files only. Third-party runtime components retain their original licenses and notices. The project does not redistribute the NetEase Cloud Music application, its libraries, user music, or DAC drivers.

## Bundled runtime components

| Component | Pinned version | Upstream license / source |
| --- | --- | --- |
| CPython Windows embeddable distribution | 3.12.10, x64 | [Versioned Python license](https://github.com/python/cpython/blob/v3.12.10/LICENSE), [CPython v3.12.10 source](https://github.com/python/cpython/tree/v3.12.10) |
| Frida Python bindings and native runtime | 17.25.1 | [Upstream COPYING](https://github.com/frida/frida/blob/17.25.1/COPYING): wxWindows Library Licence 3.1, based on GNU Library GPL v2-or-later with the stated exception; [Frida source](https://github.com/frida/frida/tree/17.25.1), [Python bindings source](https://github.com/frida/frida-python/tree/17.25.1) |
| websocket-client | 1.9.2 | [Apache License 2.0](https://github.com/websocket-client/websocket-client/blob/v1.9.2/LICENSE), [versioned source](https://github.com/websocket-client/websocket-client/tree/v1.9.2) |

Versions describe the intended pinned build inputs, not a claim that upstream's newest releases have been adopted. See the build manifests and each published artifact's checksums for the inputs actually used.

## License files in distributions

- Preserve CPython's complete `LICENSE.txt`. It contains Python's license history and additional notices relevant to that distribution.
- Preserve the Frida distribution's `COPYING`, its referenced `COPYING.LIB`, and any additional notices supplied with the native runtime. The wxWindows exception is part of the upstream text; do not omit it or replace the component's license with the project's MIT license.
- Preserve websocket-client's `LICENSE` and any NOTICE / copyright information supplied by the upstream version.
- Keep component license files readable in both the installer payload and the portable package. Build-time tools and GitHub Actions are separate from the installed runtime.

The local wheel metadata and license files for Frida 17.25.1 and websocket-client 1.9.2 were checked when this project was prepared. The upstream links above identify exact releases. A binary runtime may include further upstream dependencies; retain those notices rather than assuming the top-level package license is the complete inventory.

## Source availability

Official versioned source is available from the links above. The Frida superproject uses submodules: use a recursive checkout and retain the recorded submodule revisions when preparing a source bundle. An automatically generated GitHub source archive alone does not carry those submodule contents.

The current build redistributes the unmodified upstream Frida wheel and retains its wxWindows exception. These entries record precise source provenance; this repository does not add an unbacked written source offer. If future releases modify upstream components or change their distribution, review the applicable terms and preserve any necessary matching source, build material and patches. The practical release checklist is in [docs/RELEASING.md](docs/RELEASING.md).

## Project modifications

The extension supplies its own Python, JavaScript and C# code and uses the bundled runtime through its public interfaces. No modification of Frida, CPython or websocket-client is required by the current build. If that changes, record the modified files and patch provenance in the corresponding release.

The project-authored C/assembly autoload module uses Windows system libraries. It is compiled with the Zig 0.13.0 build tool, distributed by the upstream `ziglang/zig-pypi` project. The compiler itself is not bundled in the installed application. The native module's mingw-w64 runtime notices are preserved in `licenses/mingw-w64-COPYING`.
