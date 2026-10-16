# Tahoe development branch

This fork branch starts at upstream `b737dedc11fa4edbcb4abe8d51e94cebfed21ac6`
(2.0.4). The original fork head, `cd8e2bb04da48d2fa70cf1895879608f3588dbfb`,
is an ancestor 142 commits behind that revision. Security, sleep/wake and
Tahoe runtime changes in those commits are upstream work, not new fixes by
this fork. The fork's main branch is not modified.

## Changes made here

- `tools/vtdump.py` now reads the big-endian universal header correctly and
  supports both FAT32 and FAT64 wrappers. It selects exactly one x86_64 slice
  and checks input bounds before interpreting load commands, symbols and
  relocations. ARM-only and ambiguous images are refused. Chained fixups are
  explicitly unsupported instead of being interpreted as classic pointers.
- `gen_tahoe_fwd.py` matches the complete parameter signature, including const,
  rather than choosing the first overload with the same number of parameters.
  It checks the demangler's result, refuses unhandled missing symbols and
  replaces the generated header only after successful generation. A failed
  generation leaves the previous header intact.
- `tools/test_tahoe_tools.py` tests the real reader with synthetic Mach-O files,
  generation failure cases, and declaration coverage for the 68 checked-in
  forwarders. On macOS it also compiles and reads a tiny non-installed kext
  binary with Apple clang. The workflow runs the suite on Windows, Linux and
  macOS; the macOS runner is a toolchain check, not a GPU test.

These fixes improve the tools used to investigate and rebuild Tahoe support.
They do not modify the shipped forwarding offsets, GPU voltage, clocks, fan
control, firmware, OS version gates or the kernel's runtime behavior. An
existing downloadable driver is not rebuilt by this branch. Building the
1401 app alone still uses the upstream package URL embedded in its source.

## Local regression check

```sh
python3 tools/test_tahoe_tools.py -v
```

Only Python's standard library is needed except for the macOS-only compiler
test. To regenerate the header on a development Mac, use a known Sequoia
NVAccel build and a reviewed list of missing mangled symbols:

```sh
python3 kexts/NVRM/accel/gen_tahoe_fwd.py /path/to/NVAccel /path/to/missing-symbols.txt
```

The reader still expects a zero-linked kext using classic relocations. It is
not a general kernel-collection reader. Matching a header declaration does
not prove that a target macOS build has the same vtable layout. The existing
forwarders rely on the layout measured by upstream for its stated KDK build;
compare the relevant base vtables in the exact target KDK before rebuilding
for a different macOS build. If ordering or method ABI changes, stop and
review the interface; a successful link alone is insufficient.

## Validation recorded on 2026-10-16

- Windows / Python 3.12: 15 tests passed; the Apple clang test was skipped.
  Before the fixes, the overload, demangler-failure and unhandled-symbol
  regression cases all failed against the original generator.
- The upstream 2.0.4 package was downloaded without installing or executing
  its contents. Its SHA-256 was
  `ae307a431cb0e964757c15ed68d090508e2d85d01160599fbe9b899ae9ccf81b`, matching
  the value in the pinned upstream app source.
- The real macOS 15 NVAccel binary (238056 bytes) and macOS 26 NVAccel binary
  (240584 bytes) each yielded 30 vtables / 3846 slots. Every decoded table
  matched the original reader's result. Wrapping each binary in synthetic
  FAT32 and FAT64 containers produced exactly the same decoded tables.
  The containers test slice extraction; they are not new driver builds.
- [GitHub Actions run 38909447175](https://github.com/HaeyNav/nvidia-macos-driver/actions/runs/38909447175)
  passed on Windows, Linux and macOS for code commit
  `4d33b94b8c11d57e2468ff6e11b9252e049f4a57`. The macOS 15.7.9 ARM64 runner
  passed all 16 tests, including Apple clang cross-compiling a minimal
  x86_64 kext and parsing its vtable. Windows and Linux skip that compiler
  test. This is not a test of loading the driver on Apple Silicon.
  Neither a full NVIDIA driver build nor a Tahoe boot/render/sleep test
  was performed.

## Required before calling a driver build validated on Tahoe

1. Record the exact macOS product/build versions, Xcode and KDK versions,
   GPU PCI ID/model, motherboard or laptop, displays, and OpenCore revision.
2. Build NVRM/NVRMFB and both macOS accelerator variants from matching source
   and headers. Use `build/accel_build_all.sh` for the two NVAccel variants;
   it still requires the upstream NVIDIA source/build prerequisites.
3. On the target macOS installation, verify all four NVIDIA kexts link into
   a scratch auxiliary kernel collection. The existing
   `tools/prove_tahoe_kmutil.sh` checks the separate AMD/Intel/NPU path and
   must not be reported as proof for the NVIDIA driver.
4. Use a recoverable test installation for cold boot, Metal rendering,
   display mode changes, hot plug and repeated sleep/wake checks. Record
   panics, GPU faults and rendering correctness, not only frame rate.
5. Check temperature, power and fan behavior with trustworthy telemetry on
   the actual machine before sustained workloads. These tool tests provide
   no evidence of thermal safety or support for untested GPU models.

No real NVIDIA/Tahoe machine has been tested as part of these fork changes.
No support for macOS 27+, Apple Silicon or previously unsupported GPU
architectures is claimed. These changes were AI-assisted; they are maintained
in this fork and are not an upstream contribution.
