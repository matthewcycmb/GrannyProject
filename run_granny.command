#!/bin/zsh
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'The project environment is missing. Install requirements.txt into .venv first.'
  exit 1
fi
exec .venv/bin/python granny_app.py "$@"
