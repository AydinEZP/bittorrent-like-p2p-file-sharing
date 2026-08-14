# Limitations and reproducibility

## Reproducibility scope

The project uses Python's standard library and loopback networking. The component and integration tests create their own small deterministic inputs and temporary output trees. No external dataset, network service, or package download is required.

The supplied implementation targets Python 3.10 or newer. Exact operating-system and Python patch versions were not specified in the original project materials, so no narrower version claim is made.

## Protocol scope

This implementation demonstrates the separation between tracker-based peer discovery and direct peer-to-peer transfer. It is inspired by BitTorrent concepts but is not wire-compatible with standard `.torrent` clients:

- metainfo is represented as JSON;
- tracker messages use Bencode, but peer messages use length-prefixed JSON;
- piece bytes are Base64-encoded inside peer messages;
- piece integrity uses SHA-1 hashes from the metainfo;
- transfer is demonstrated on loopback networking.

Rarest-first selection, end-game mode, tit-for-tat, NAT traversal, transport encryption, authentication, and Internet-scale deployment are outside the demonstrated scope.

## Publication boundaries

Generated logs, test outputs, nested submission archives, bytecode caches, old report exports, and screenshots containing absolute local paths are excluded. The repository includes four loopback packet-capture screenshots that contain no student identifier, email address, credential, or private local path.

No software license was present in the supplied materials. Public visibility should not be interpreted as permission for unrestricted reuse.
