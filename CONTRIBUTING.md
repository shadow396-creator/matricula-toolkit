# Contributing

Contributions to the Matricula Toolkit are welcome! Here's how to get started.

## Setting up the development environment

```bash
# Clone the repository
git clone <repository-url>
cd matricula-toolkit

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the package with dev dependencies
pip install -e ".[dev]"

# Install Chromium for Playwright
playwright install chromium
```

## Running tests

```bash
pytest
```

Run with coverage:

```bash
pytest --cov=src/matricula_toolkit
```

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for issues
ruff check src/ tests/

# Format code
ruff format src/ tests/

# Check and fix automatically
ruff check --fix src/ tests/
```

We use [mypy](https://mypy.readthedocs.io/) for type checking:

```bash
mypy src/
```

## Submitting changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Add or update tests
5. Run the test suite (`pytest`)
6. Commit your changes (`git commit -m "Add your feature"`)
7. Push to your fork (`git push origin feature/your-feature`)
8. Open a pull request

## Reporting issues

Please use the GitHub issue tracker to report bugs or request features.
Include:

- Your operating system and Python version
- The Matricula book URL you're trying to download
- The command you ran and the error message
- Any relevant log output

## License

By contributing, you agree that your contributions will be licensed under
the MIT License (see LICENSE file).