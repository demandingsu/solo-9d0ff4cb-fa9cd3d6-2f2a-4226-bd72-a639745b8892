#!/usr/bin/env bash
# 端到端冒烟：起一个临时库的服务，走一遍报送、两种互持、补报、替换、查询。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
Q=2026Q3

post() { curl -sS -X POST "$BASE/funds/holdings" -H 'Content-Type: application/json' -d "$1"; echo; }
get() { curl -sS "$BASE$1"; echo; }

echo "== 健康检查 =="
get "/health"

echo "== 报送 C（先于其底层基金，制造缺报）=="
post "{\"quarter\":\"$Q\",\"fund_code\":\"C\",\"rows\":[{\"target_type\":\"fund\",\"target_code\":\"A\",\"weight_bp\":5000},{\"target_type\":\"security\",\"target_code\":\"S3\",\"weight_bp\":5000}]}"

echo "== 报送 A、B：无出口互持组（全封闭）=="
post "{\"quarter\":\"$Q\",\"fund_code\":\"A\",\"rows\":[{\"target_type\":\"fund\",\"target_code\":\"B\",\"weight_bp\":10000}]}"
post "{\"quarter\":\"$Q\",\"fund_code\":\"B\",\"rows\":[{\"target_type\":\"fund\",\"target_code\":\"A\",\"weight_bp\":10000}]}"

echo "== C 的穿透：5000 S3 + 5000 流入封闭组未穿透 =="
get "/quarters/$Q/funds/C/penetration"

echo "== 替换 B，给互持组开一个证券出口（有出口互持，按比例分摊）=="
post "{\"quarter\":\"$Q\",\"fund_code\":\"B\",\"replace\":true,\"rows\":[{\"target_type\":\"fund\",\"target_code\":\"A\",\"weight_bp\":5000},{\"target_type\":\"security\",\"target_code\":\"S9\",\"weight_bp\":5000}]}"

echo "== A 现在应 100% 落到 S9，无未穿透；重算范围含 A、B、C =="
get "/quarters/$Q/funds/A/penetration"

echo "== 补报缺报场景：D -> E（缺报），再补报 E =="
post "{\"quarter\":\"$Q\",\"fund_code\":\"D\",\"rows\":[{\"target_type\":\"fund\",\"target_code\":\"E\",\"weight_bp\":4000},{\"target_type\":\"security\",\"target_code\":\"SD\",\"weight_bp\":4000}]}"
get "/quarters/$Q/funds/D/penetration"
post "{\"quarter\":\"$Q\",\"fund_code\":\"E\",\"rows\":[{\"target_type\":\"security\",\"target_code\":\"SE\",\"weight_bp\":10000}]}"
get "/quarters/$Q/funds/D/penetration"

echo "== 未穿透全季度总计 =="
get "/quarters/$Q/unpenetrated"

echo "== 重复报送无 replace -> 409；超 10000 -> 422；未知基金 -> 404 =="
curl -sS -o /dev/null -w "conflict=%{http_code}\n" -X POST "$BASE/funds/holdings" -H 'Content-Type: application/json' \
  -d "{\"quarter\":\"$Q\",\"fund_code\":\"D\",\"rows\":[]}"
curl -sS -o /dev/null -w "overweight=%{http_code}\n" -X POST "$BASE/funds/holdings" -H 'Content-Type: application/json' \
  -d "{\"quarter\":\"$Q\",\"fund_code\":\"Z\",\"rows\":[{\"target_type\":\"security\",\"target_code\":\"SX\",\"weight_bp\":10001}]}"
curl -sS -o /dev/null -w "notfound=%{http_code}\n" "$BASE/quarters/$Q/funds/GHOST/penetration"
