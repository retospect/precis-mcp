#!/bin/sh
# Build the status deck. Tectonic preferred (once its CDN is back); docker fallback works today.
cd "$(dirname "$0")"
if tectonic slides.tex 2>/dev/null; then exit 0; fi
docker run --rm -v "$PWD":/work -w /work texlive/texlive:latest-medium \
  sh -c "pdflatex -interaction=nonstopmode slides.tex && pdflatex -interaction=nonstopmode slides.tex"
