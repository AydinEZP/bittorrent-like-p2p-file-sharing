# Report source

This directory is the LaTeX source for the project report. It is derived from the supplied `latex-template.zip` and retains that template's article class, NewTX typography, page geometry, title-page design, color system, headers, footers, section styles, captions, numbering, bibliography style, and reusable figure and code environments.

Project-specific values are isolated in `metadata.tex`. The report uses only evidence present in the supplied project materials and the local validation run.

## Local build

From this directory:

```text
make
```

The Makefile writes all auxiliary files and the PDF to `build/`. The release copy intentionally includes `report.pdf` but excludes `build/`.
