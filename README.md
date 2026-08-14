# Simplified BitTorrent-like P2P File Sharing System

This university project implements a compact peer-to-peer file-sharing system in Python. A multithreaded HTTP tracker maintains swarm membership and returns peers; file pieces travel directly between peers over a custom framed TCP protocol. Each piece is checked with SHA-1 before it is written.

The implementation is intentionally educational rather than a full BitTorrent client. It uses human-readable JSON metainfo, Base64-encoded piece payloads, and a focused set of tracker and peer messages.

## Main capabilities

- Single-file and multi-file metainfo generation and validation
- Deterministic Bencode support for tracker messages and `info_hash` calculation
- Multithreaded HTTP tracker with peer expiry and swarm statistics
- Direct peer PING/PONG, bitfield, interest, request, and piece messages over TCP
- Piece-boundary handling across multiple files
- SHA-1 verification before storage
- Independent worker threads for multiple torrents
- Thread-safe event logging
- Standard-library-only implementation and test runner

## Requirements

- Python 3.10 or newer
- No third-party Python packages
- Windows command scripts are provided for the guided demo; the Python modules are also directly runnable on other platforms

## Run the test suite

```text
python run_all_tests.py
```

The suite uses only loopback networking and writes generated data under `test_data/` and `test_output/`. These paths are ignored by Git.

The release candidate was statically compiled and all nine supplied test groups passed in an isolated copy on 2026-08-14. The validation covered metainfo, logging, tracker state, tracker HTTP, tracker-client behavior, peer PING/PONG, piece transfer, concurrent torrent workers, and final integration. No external service or dataset was used.

## Run the integrated demo

For an automated local demonstration:

```text
python -m tools.end_to_end_demo --workspace demo_workspace_integrated --reset
```

For a manual Windows demonstration, run the commands in `demo/` in numeric order. The tracker and peer processes bind to loopback by default.

## Repository structure

```text
common/          Bencode and thread-safe logging utilities
peer/            Peer protocol, piece storage, tracker client, and workers
tracker/         HTTP tracker service and synchronized swarm state
tools/           Metainfo, demo, verification, and submission utilities
tests/           Component and end-to-end tests
demo/            Windows command scripts for a multi-terminal demonstration
report/          Template-based LaTeX source and compiled report
data/            Data and provenance notes
docs/            Limitations and reproducibility notes
```

## Documented results

The supplied report records successful reconstruction of a 4,099-byte single-file payload and a 3,164-byte multi-file bundle. The included tests cover metainfo validation, tracker state and HTTP behavior, tracker-client encoding, framed peer messaging, piece transfer, concurrent torrent workers, and full local integration. See [the technical report](report/report.pdf) for the design and validation details.

## Limitations

This is not a wire-compatible BitTorrent implementation. The project does not implement rarest-first selection, end-game mode, tit-for-tat, NAT traversal, transport encryption, or production-grade authentication. See [limitations and reproducibility](docs/limitations-and-reproducibility.md).

## Authorship and reuse

Project author: AydinEZP.

No license is supplied with the available project materials. Repository visibility does not itself grant permission to reuse or redistribute the code.
