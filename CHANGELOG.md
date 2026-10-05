# Changelog

## v0.4.0 - 2026-10-05

- Added protected physical-card file writes for updating `.elf` and other files.
- Added automatic full-card safety backups before every write.
- Added changed-card and capacity checks before programming begins.
- Added PS2 spare-data ECC generation and page-by-page read-back verification.
- Added recovery for PowerWave adapters using the negotiated `0x55` terminator.
- Kept writes unavailable while browsing standalone backup images.

## v0.3.0 - 2026-08-22

- Added semantic version numbers to the application and AppImage builds.
- Added the PS2-inspired 3D memory card browser.
- Rendered each save from its own textured PS2 icon model.
- Improved icon isolation, animation caching, frame rate, and corrupt-model
  fallback handling.
- Improved live reads, adapter resets, card-swap recovery, and 64 MB card
  support for the PowerWave adapter.
- Added full `.ps2` backups and `.psu` save export.
- Renamed the project to Memory Prism.
