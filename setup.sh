#!/usr/bin/env bash
# exit on error
set -o errexit

# Force Playwright to download browsers into the local project directory
# so they aren't wiped out by Render's build cache clearer.
export PLAYWRIGHT_BROWSERS_PATH=0

pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
