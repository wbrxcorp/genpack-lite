.PHONY: all install clean

PREFIX ?= /usr/local
PYTHON_USER_SITE=$(shell python -c "import site; print(site.getusersitepackages())")

all: src/genpack.py src/genpack-helper.bin
	@echo "Use 'sudo make install-helper', 'make install' to install genpack-helper and genpack."

src/genpack-helper.bin: src/genpack-helper.cpp
	@echo "Compiling genpack-helper.cpp to genpack-helper.bin"
	g++ -std=c++20 -o $@ $< -lmount

install-helper: src/genpack-helper.bin
	@echo "Installing genpack-helper binary to $(DESTDIR)$(PREFIX)/bin"
	mkdir -p $(DESTDIR)$(PREFIX)/bin
	cp -a src/genpack-helper.bin $(DESTDIR)$(PREFIX)/bin/genpack-helper
	chown root:root $(DESTDIR)$(PREFIX)/bin/genpack-helper
	chmod +s $(DESTDIR)$(PREFIX)/bin/genpack-helper
	@echo "Installation of genpack-helper complete."

install: src/genpack.py
	@echo "Installing genpack to $(PYTHON_USER_SITE)"
	mkdir -p $(PYTHON_USER_SITE)
	cp src/genpack.py $(PYTHON_USER_SITE)/genpack.py
	@echo "Installation complete. You can now run 'python -m genpack' from the command line."

clean:
	@echo "Cleaning up..."
	rm -f src/genpack-helper.bin
	rm -rf src/__pycache__
