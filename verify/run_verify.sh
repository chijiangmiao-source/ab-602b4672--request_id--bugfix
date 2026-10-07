#!/usr/bin/env bash
# Verify pipeline: envelope contract tests -> build check -> HTTP smoke.
# The container exit code is the acceptance result.
set -u
cd /verify

echo "=== [1/3] envelope contract tests (包络契约测试) ==="
python -m pytest tests/test_envelope_contract.py -q || { echo "VERIFY FAILED: contract tests"; exit 1; }

echo "=== [2/3] build check (构建检查) ==="
python -m compileall -q packager || { echo "VERIFY FAILED: compileall"; exit 1; }
python -c "import packager.server" || { echo "VERIFY FAILED: server import"; exit 1; }

echo "=== [3/3] HTTP smoke (字段保留 / 冲突 / 健康接口) ==="
python tests/smoke.py || { echo "VERIFY FAILED: smoke"; exit 1; }

echo "VERIFY OK: all acceptance checks passed"
exit 0
