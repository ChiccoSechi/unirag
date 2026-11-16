#!/usr/bin/env bash
# Creates the Vectorize index and the metadata indexes.
# Run it ONCE, BEFORE inserting any vector:
# vectors inserted before a metadata index exists are not filtered.
# From the main folder: wrangler (installed in bot/) reads the API token from .env there.
set -euo pipefail

INDEX="${VECTORIZE_INDEX:-unirag-inf}"

npx --prefix bot wrangler vectorize create "$INDEX" --dimensions=1024 --metric=cosine
npx --prefix bot wrangler vectorize create-metadata-index "$INDEX" --property-name=year --type=string
npx --prefix bot wrangler vectorize create-metadata-index "$INDEX" --property-name=kind --type=string
npx --prefix bot wrangler vectorize create-metadata-index "$INDEX" --property-name=valid_until --type=number
npx --prefix bot wrangler vectorize create-metadata-index "$INDEX" --property-name=page --type=string
