#!/usr/bin/env bash
# Example: Download pages 104-200 for book 01519

set -euo pipefail

# Activate virtual environment
source .venv/bin/activate

# Set environment variables
export MATRICULA_BASE_URL="https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/"
export MATRICULA_BOOK="01519"
export MATRICULA_OUT_DIR="./downloads"
export MATRICULA_WAIT_SECONDS="5"
export MATRICULA_HEADLESS="true"

# Download pages 104-200
matricula-download range 104 200