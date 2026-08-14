# Data and generated payloads

The project does not use an external dataset. Its tests and demo utilities generate small deterministic binary and text payloads locally, then create JSON metainfo files from those payloads.

Generated `test_data/`, `test_output/`, and demo workspaces are intentionally excluded from the repository. Running the test suite or demo recreates the needed local files. No third-party data is redistributed.
