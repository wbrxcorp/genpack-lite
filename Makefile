.PHONY: all install clean

PYTHON_USER_SITE=$(shell python -c "import site; print(site.getusersitepackages())")

all:
	@echo "Use 'make install' to install the script."

install: src/genpack.py
	@echo "Installing genpack to $(PYTHON_USER_SITE)"
	mkdir -p $(PYTHON_USER_SITE)
	cp src/genpack.py $(PYTHON_USER_SITE)/genpack.py
	@echo "Installation complete. You can now run 'python -m genpack' from the command line."
