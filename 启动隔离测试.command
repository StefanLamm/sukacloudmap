#!/bin/zsh
cd "$(dirname "$0")"
exec .venv/bin/python scripts/manage-services.py start trial
