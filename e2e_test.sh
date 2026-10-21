#!/usr/bin/env bash
# PaySync end-to-end check. Usage: bash e2e_test.sh   (no dependencies for steps 1-4)
set -e
cd "$(dirname "$0")"
echo "=== [1/4] Core integrity ===";   python3 verify_core.py
echo "=== [2/4] Regression probes ==="; python3 audit_engine.py
echo "=== [3/4] Unit + API tests ===";  python3 run_tests.py
echo "=== [4/4] Live demo ===";         python3 demo/demo.py
echo "ALL CHECKS PASSED"
