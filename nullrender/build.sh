#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
x86_64-w64-mingw32-g++ -shared -static -static-libgcc -static-libstdc++ -O2 \
  -o d3d11.dll d3d11_proxy.cpp
cp -f d3d11.dll ../docker/game/d3d11.dll
