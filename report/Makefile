BUILD_DIR := build
MAIN := main

.PHONY: all clean

all:
	mkdir -p $(BUILD_DIR)
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=$(BUILD_DIR) $(MAIN).tex
	BIBINPUTS=.: bibtex $(BUILD_DIR)/$(MAIN)
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=$(BUILD_DIR) $(MAIN).tex
	pdflatex -interaction=nonstopmode -halt-on-error -output-directory=$(BUILD_DIR) $(MAIN).tex

clean:
	rm -rf $(BUILD_DIR)
